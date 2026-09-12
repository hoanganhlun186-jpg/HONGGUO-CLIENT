"""Lossless subtitle packets and validated shared conventions for Gemini web."""
import hashlib, json, os, re, tempfile

SECTIONS = ('boi_canh','van_phong','nhan_vat','xung_ho_hai_chieu','thay_doi_quan_he','thuat_ngu','chua_ro')

def natural(path):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)',path)]

def packets(items, parse_srt, limit=12000):
    records=[]
    seen=set()
    for item in sorted(items,key=lambda i:natural(os.path.abspath(i['srt']))):
        path=os.path.abspath(item['srt'])
        if os.path.normcase(path) in seen: continue
        seen.add(os.path.normcase(path))
        with open(path,encoding='utf-8-sig') as f: content=f.read()
        blocks=parse_srt(content)
        if content.strip() and not blocks: raise ValueError(f'SRT không đọc được: {path}')
        timecodes=re.findall(r'^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->.*$',content,re.M)
        if len(timecodes)!=len(blocks): raise ValueError(f'SRT có câu lỗi định dạng, không gửi bản thiếu: {path}')
        ep=len(seen)
        records.append(f'=== TẬP {ep}: {os.path.basename(path)} ===')
        if not blocks: records.append('(Tập không có lời thoại)')
        for b in blocks:
            records.append(f'[E{ep}:C{b["stt"]}] {b["text"]}')
    if not records: raise ValueError('Không có SRT để phân tích')
    # Split even an unusually long single cue without dropping any characters.
    result=[]; current=''
    for record in records:
        remaining=record+'\n'
        while remaining:
            take=min(limit-len(current),len(remaining))
            current+=remaining[:take];remaining=remaining[take:]
            if len(current)==limit: result.append(current);current=''
    if current: result.append(current)
    return result, len(seen)

def decode_rules(response, count):
    text=(response or '').strip().lstrip('\ufeff')
    if not text: raise ValueError('Gemini trả phản hồi trống')
    if 'LOI_THIEU_DU_LIEU' in text.replace('\\_', '_'):
        raise ValueError('Gemini báo thiếu dữ liệu; không nhận quy ước lẫn mã lỗi')
    decoder=json.JSONDecoder()
    candidates=[]
    errors=[]
    # Allow explanatory prose/Markdown around a complete JSON object.
    # Do not manufacture missing fields or accept an unfinished JSON object.
    for match in re.finditer(r'\{',text):
        try: data,_=decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError: continue
        if not isinstance(data,dict) or 'received_parts' not in data: continue
        if data.get('received_parts')!=list(range(1,count+1)):
            errors.append('Gemini chưa xác nhận đủ các lượt SRT');continue
        rules=data.get('rules')
        if not isinstance(rules,dict) or any(not rules.get(k) or not isinstance(rules[k],(str,list,dict)) for k in SECTIONS):
            errors.append('Bộ quy ước thiếu mục bắt buộc');continue
        candidates.append(rules)
    if len(candidates)==1: return candidates[0]
    if len(candidates)>1: raise ValueError('Gemini trả nhiều bộ quy ước; cần một kết quả duy nhất')
    raise ValueError(errors[0] if errors else 'Gemini chưa trả bộ quy ước JSON hoàn chỉnh')

def fingerprint(parts,notes):
    return hashlib.sha256(('v2-address-evidence\n'+notes+'\n'+''.join(parts)).encode('utf-8')).hexdigest()

def save_rules(path,signature,rules,parts,episodes):
    fd,tmp=tempfile.mkstemp(prefix='.context_',suffix='.json',dir=os.path.dirname(path))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump({'version':1,'signature':signature,'episodes':episodes,'parts':parts,'rules':rules},f,ensure_ascii=False,indent=2)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.remove(tmp)
