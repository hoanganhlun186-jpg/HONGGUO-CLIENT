"""Portable session data; credentials stay in the application's existing settings."""
import json, os, tempfile
from PyQt6.QtWidgets import QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit

def controls(owner):
    data = {}
    for name, widget in vars(owner).items():
        if any(word in name.lower() for word in ('key', 'password', 'token', 'secret')):
            continue
        if isinstance(widget, QComboBox):
            data[name] = {'kind':'combo', 'value':widget.currentText()}
        elif isinstance(widget, QCheckBox):
            data[name] = {'kind':'check', 'value':widget.isChecked()}
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            data[name] = {'kind':'number', 'value':widget.value()}
        elif isinstance(widget, QLineEdit):
            data[name] = {'kind':'text', 'value':widget.text()}
        elif isinstance(widget,QPlainTextEdit) and 'log' not in name.lower():
            data[name] = {'kind':'plain', 'value':widget.toPlainText()}
    return data

def restore_controls(owner, data):
    for name, entry in data.items():
        if any(word in name.lower() for word in ('key', 'password', 'token', 'secret')):
            continue
        w = getattr(owner, name, None)
        value = entry.get('value')
        if entry.get('kind') == 'combo' and isinstance(w, QComboBox):
            index = w.findText(str(value))
            if index >= 0: w.setCurrentIndex(index)
        elif entry.get('kind') == 'check' and isinstance(w, QCheckBox): w.setChecked(bool(value))
        elif entry.get('kind') == 'number' and isinstance(w, (QSpinBox,QDoubleSpinBox)):
            w.setValue(float(value) if isinstance(w,QDoubleSpinBox) else int(value))
        elif entry.get('kind') == 'text' and isinstance(w,QLineEdit): w.setText(str(value))
        elif entry.get('kind') == 'plain' and isinstance(w,QPlainTextEdit): w.setPlainText(str(value))

def snapshot(pages):
    rows=[]
    for p in pages:
        if p.selected_card is not None and not getattr(p,'_batch_managed',False):
            p._save_design_to_card(p.selected_card)
        rows.append({'name':p._series_name,'state':getattr(p,'_queue_state','waiting'),
            'render':controls(p),'dub':controls(p.dub_feature_tab) if getattr(p,'dub_feature_tab',None) else {},
            'default_design':p._default_design(),
            'thumbnail_x':getattr(p,'_part_badge_x',.5),'thumbnail_y':getattr(p,'_part_badge_y',.55),
            'thumb_source':getattr(p,'_thumb_src_path',None),'thumb_srt':getattr(p,'_thumb_srt_path',None),
            'cards':[{'video':c.video_path,'srt':c.srt_path,'selected':c.chk_select.isChecked(),
                      'design':getattr(c,'design_config',None)} for c in p.cards]})
    return {'version':1,'series':rows}

def save_session(path, pages):
    data=snapshot(pages)
    folder=os.path.dirname(os.path.abspath(path))
    fd,tmp=tempfile.mkstemp(prefix='.boom_session_',suffix='.json',dir=folder)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.remove(tmp)

def read_session(path):
    if os.path.getsize(path)>50*1024*1024: raise ValueError('File phiên quá lớn')
    with open(path,encoding='utf-8') as f: data=json.load(f)
    if data.get('version')!=1 or not isinstance(data.get('series'),list): raise ValueError('Sai định dạng phiên')
    if len(data['series'])>100: raise ValueError('Tối đa 100 bộ trong một phiên')
    for row in data['series']:
        if not isinstance(row,dict) or not isinstance(row.get('cards'),list): raise ValueError('Dữ liệu bộ không hợp lệ')
        for card in row['cards']:
            if not isinstance(card.get('video'),str): raise ValueError('Thiếu đường dẫn video')
    return data['series']
