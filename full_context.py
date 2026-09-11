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
    text=response.strip()
    if text.startswith('```'): text=re.sub(r'^```(?:json)?\s*','',text);text=re.sub(r'\s*```$','',text)
    data=json.loads(text)
    if data.get('received_parts')!=list(range(1,count+1)): raise ValueError('Gemini chưa xác nhận đủ các lượt SRT')
    rules=data.get('rules')
    if not isinstance(rules,dict) or any(not rules.get(k) for k in SECTIONS):
        raise ValueError('Bộ quy ước thiếu mục bắt buộc')
    return rules

def fingerprint(parts,notes):
    return hashlib.sha256(('v1\n'+notes+'\n'+''.join(parts)).encode('utf-8')).hexdigest()

def save_rules(path,signature,rules,parts,episodes):
    fd,tmp=tempfile.mkstemp(prefix='.context_',suffix='.json',dir=os.path.dirname(path))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump({'version':1,'signature':signature,'episodes':episodes,'parts':parts,'rules':rules},f,ensure_ascii=False,indent=2)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.remove(tmp)
