"""cenc_decrypt — giải MP4 progressive. Narrative thuật toán ở docs (ko để trong file ship)."""
import sys
import struct

from Crypto.Cipher import AES
from Crypto.Util import Counter


def read_box_header(buf, pos):
    size = struct.unpack_from(">I", buf, pos)[0]
    btype = bytes(buf[pos + 4:pos + 8])
    hlen = 8
    if size == 1:
        size = struct.unpack_from(">Q", buf, pos + 8)[0]
        hlen = 16
    elif size == 0:
        size = len(buf) - pos
    return size, btype, hlen, pos + size


def iter_boxes(buf, start, end):
    pos = start
    while pos + 8 <= end:
        size, btype, hlen, box_end = read_box_header(buf, pos)
        if size <= 0 or box_end > end:
            break
        yield btype, pos, pos + hlen, box_end
        pos = box_end


def find_box(buf, start, end, target):
    for btype, bs, cs, ce in iter_boxes(buf, start, end):
        if btype == target:
            return bs, cs, ce
    return None


def find_all(buf, start, end, target):
    out = []
    for btype, bs, cs, ce in iter_boxes(buf, start, end):
        if btype == target:
            out.append((bs, cs, ce))
    return out


def parse_stsz(buf, cs, ce):
    sample_size = struct.unpack_from(">I", buf, cs + 4)[0]
    count = struct.unpack_from(">I", buf, cs + 8)[0]
    if sample_size != 0:
        return [sample_size] * count
    sizes = list(struct.unpack_from(">%dI" % count, buf, cs + 12))
    return sizes


def parse_stco(buf, cs, ce, is64):
    count = struct.unpack_from(">I", buf, cs + 4)[0]
    if is64:
        return list(struct.unpack_from(">%dQ" % count, buf, cs + 8))
    return list(struct.unpack_from(">%dI" % count, buf, cs + 8))


def parse_stsc(buf, cs, ce):
    count = struct.unpack_from(">I", buf, cs + 4)[0]
    entries = []
    off = cs + 8
    for _ in range(count):
        fc, spc, sdi = struct.unpack_from(">III", buf, off)
        entries.append((fc, spc, sdi))
        off += 12
    return entries


def build_sample_offsets(stsc, chunk_offsets, sample_sizes):
    n_samples = len(sample_sizes)
    n_chunks = len(chunk_offsets)
    spc_per_chunk = [0] * n_chunks
    for i, (fc, spc, sdi) in enumerate(stsc):
        first = fc - 1
        last = (stsc[i + 1][0] - 1) if i + 1 < len(stsc) else n_chunks
        for c in range(first, min(last, n_chunks)):
            spc_per_chunk[c] = spc

    out = []
    sidx = 0
    for c in range(n_chunks):
        off = chunk_offsets[c]
        for _ in range(spc_per_chunk[c]):
            if sidx >= n_samples:
                break
            sz = sample_sizes[sidx]
            out.append((off, sz))
            off += sz
            sidx += 1
    assert len(out) == n_samples, (len(out), n_samples)
    return out


def parse_senc(buf, cs, ce, iv_size, n_samples):
    flags = struct.unpack_from(">I", buf, cs)[0] & 0x00FFFFFF
    has_subsamples = bool(flags & 0x000002)
    count = struct.unpack_from(">I", buf, cs + 4)[0]
    off = cs + 8
    out = []
    for _ in range(count):
        iv = buf[off:off + iv_size]
        off += iv_size
        iv16 = iv + b"\x00" * (16 - len(iv))
        subs = None
        if has_subsamples:
            sub_count = struct.unpack_from(">H", buf, off)[0]
            off += 2
            subs = []
            for _ in range(sub_count):
                clear, enc = struct.unpack_from(">HI", buf, off)
                off += 6
                subs.append((clear, enc))
        out.append((iv16, subs))
    return out


def decrypt_sample(key, iv16, sample_data, subs):
    initial = int.from_bytes(iv16, "big")
    ctr = Counter.new(128, initial_value=initial)
    cipher = AES.new(key, AES.MODE_CTR, counter=ctr)

    data = bytearray(sample_data)
    if not subs:
        return cipher.decrypt(bytes(data))

    pos = 0
    for clear, enc in subs:
        pos += clear
        if enc:
            chunk = bytes(data[pos:pos + enc])
            dec = cipher.decrypt(chunk)
            data[pos:pos + enc] = dec
            pos += enc
    return bytes(data)


def gather_track(buf, trak_cs, trak_ce):
    mdia = find_box(buf, trak_cs, trak_ce, b"mdia")
    if not mdia:
        return None
    _, md_cs, md_ce = mdia
    minf = find_box(buf, md_cs, md_ce, b"minf")
    _, mi_cs, mi_ce = minf
    stbl = find_box(buf, mi_cs, mi_ce, b"stbl")
    _, st_cs, st_ce = stbl

    stsd = find_box(buf, st_cs, st_ce, b"stsd")
    iv_size = None
    enc_entry_type = None
    if stsd:
        _, sd_cs, sd_ce = stsd
        for et in (b"encv", b"enca"):
            r = find_box(buf, sd_cs + 8, sd_ce, et)
            if r:
                enc_entry_type = et
                _, e_cs, e_ce = r
                tenc = try_recurse(buf, e_cs, e_ce, b"tenc")
                if tenc:
                    _, t_cs, t_ce = tenc
                    iv_size = buf[t_cs + 7]
                break
    if enc_entry_type is None:
        return None

    stsz = find_box(buf, st_cs, st_ce, b"stsz")
    sizes = parse_stsz(buf, stsz[1], stsz[2])

    co64 = find_box(buf, st_cs, st_ce, b"co64")
    if co64:
        chunk_offsets = parse_stco(buf, co64[1], co64[2], True)
    else:
        stco = find_box(buf, st_cs, st_ce, b"stco")
        chunk_offsets = parse_stco(buf, stco[1], stco[2], False)

    stsc = find_box(buf, st_cs, st_ce, b"stsc")
    stsc_entries = parse_stsc(buf, stsc[1], stsc[2])

    offsets = build_sample_offsets(stsc_entries, chunk_offsets, sizes)

    senc = find_descendant(buf, trak_cs, trak_ce, b"senc")
    senc_records = None
    if senc:
        senc_records = parse_senc(buf, senc[1], senc[2], iv_size, len(sizes))

    return {
        "type": enc_entry_type,
        "iv_size": iv_size,
        "offsets": offsets,
        "senc": senc_records,
    }


def find_descendant(buf, start, end, target):
    for btype, bs, cs, ce in iter_boxes(buf, start, end):
        if btype == target:
            return bs, cs, ce
        if btype in CONTAINER_TYPES:
            r = find_descendant(buf, cs, ce, target)
            if r:
                return r
        else:
            r = try_recurse(buf, cs, ce, target)
            if r:
                return r
    return None


def try_recurse(buf, start, end, target):
    pos = start
    found_any = False
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        btype = bytes(buf[pos + 4:pos + 8])
        if size < 8 or pos + size > end or not _is_printable(btype):
            pos += 1
            continue
        found_any = True
        if btype == target:
            return pos, pos + 8, pos + size
        if btype in CONTAINER_TYPES or btype in (b"sinf", b"schi"):
            r = find_descendant(buf, pos + 8, pos + size, target)
            if r:
                return r
        pos += size
    return None


def _is_printable(b):
    return all(0x20 <= c <= 0x7E for c in b)


CONTAINER_TYPES = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"sinf", b"schi", b"edts",
    b"dinf", b"udta", b"mvex",
}


def fix_encryption_boxes(buf):
    moov = find_box(buf, 0, len(buf), b"moov")
    if not moov:
        return
    _, mv_cs, mv_ce = moov
    for et in (b"encv", b"enca"):
        _replace_entry_type(buf, mv_cs, mv_ce, et)
    for name in (b"senc", b"saio", b"saiz"):
        for bs, cs, ce in _find_all_descendants(buf, mv_cs, mv_ce, name):
            buf[bs + 4:bs + 8] = b"free"


def _replace_entry_type(buf, start, end, et):
    for bs, cs, ce in _find_all_descendants(buf, start, end, et):
        sinf = find_descendant(buf, cs, ce, b"sinf")
        if not sinf:
            continue
        _, si_cs, si_ce = sinf
        frma = find_descendant(buf, si_cs, si_ce, b"frma")
        if not frma:
            continue
        _, fr_cs, fr_ce = frma
        orig = buf[fr_cs:fr_cs + 4]
        buf[bs + 4:bs + 8] = orig
        buf[sinf[0] + 4:sinf[0] + 8] = b"free"


def _find_all_descendants(buf, start, end, target):
    out = []

    def walk(s, e):
        for btype, bs, cs, ce in iter_boxes(buf, s, e):
            if btype == target:
                out.append((bs, cs, ce))
            if btype in CONTAINER_TYPES:
                walk(cs, ce)
            elif btype in (b"stsd",):
                walk(cs + 8, ce)
            elif btype in (b"encv", b"enca"):
                _walk_sample_entry(bs, cs, ce)

    def _walk_sample_entry(bs, cs, ce):
        pos = cs
        while pos + 8 <= ce:
            size = struct.unpack_from(">I", buf, pos)[0]
            btype = bytes(buf[pos + 4:pos + 8])
            if size < 8 or pos + size > ce or not _is_printable(btype):
                pos += 1
                continue
            if btype == target:
                out.append((pos, pos + 8, pos + size))
            if btype in (b"sinf", b"schi") or btype in CONTAINER_TYPES:
                walk(pos + 8, pos + size)
            pos += size

    walk(start, end)
    seen = set()
    uniq = []
    for t in out:
        if t[0] not in seen:
            seen.add(t[0])
            uniq.append(t)
    return uniq


def decrypt_file(in_path, key_hex, out_path, log=None):
    """Giai file -> MP4 sach. Tra so sample da xu ly. log: callable(str) tuy chon."""
    _log = log or (lambda *_: None)
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        raise ValueError("key must be 16 bytes (32 hex)")
    with open(in_path, "rb") as f:
        buf = bytearray(f.read())
    moov = find_box(buf, 0, len(buf), b"moov")
    if not moov:
        raise ValueError("khong tim thay moov box")
    _, mv_cs, mv_ce = moov
    traks = find_all(buf, mv_cs, mv_ce, b"trak")
    total = 0
    for (bs, cs, ce) in traks:
        tr = gather_track(buf, cs, ce)
        if not tr or tr.get("senc") is None:
            continue
        offsets = tr["offsets"]; senc = tr["senc"]
        if len(senc) != len(offsets):
            _log(f"  track {tr['type'].decode(errors='ignore')}: senc/offset lech, bo qua")
            continue
        for i, (foff, sz) in enumerate(offsets):
            iv16, subs = senc[i]
            dec = decrypt_sample(key, iv16, bytes(buf[foff:foff + sz]), subs)
            buf[foff:foff + sz] = dec
            total += 1
    fix_encryption_boxes(buf)
    with open(out_path, "wb") as f:
        f.write(buf)
    _log(f"  giai ma {total} sample -> {out_path}")
    return total


def main():
    if len(sys.argv) != 4:
        print("usage: cenc_decrypt.py <in> <key_hex> <out>")
        return 1
    in_path, key_hex, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    key = bytes.fromhex(key_hex)
    assert len(key) == 16, "key must be 16 bytes"

    with open(in_path, "rb") as f:
        buf = bytearray(f.read())

    moov = find_box(buf, 0, len(buf), b"moov")
    _, mv_cs, mv_ce = moov

    traks = find_all(buf, mv_cs, mv_ce, b"trak")
    total = 0
    for (bs, cs, ce) in traks:
        tr = gather_track(buf, cs, ce)
        if not tr:
            print("  track: skip")
            continue
        offsets = tr["offsets"]
        senc = tr["senc"]
        print("  track %s: %d items" % (tr["type"].decode(), len(offsets)))
        if senc is None:
            print("    WARNING: skip track")
            continue
        assert len(senc) == len(offsets), (len(senc), len(offsets))
        for i, (foff, sz) in enumerate(offsets):
            iv16, subs = senc[i]
            data = bytes(buf[foff:foff + sz])
            dec = decrypt_sample(key, iv16, data, subs)
            buf[foff:foff + sz] = dec
            total += 1

    print("  processed %d items total" % total)
    fix_encryption_boxes(buf)

    with open(out_path, "wb") as f:
        f.write(buf)
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
