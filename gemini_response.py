"""Completion evidence and strict missing-packet validation for Gemini web."""
import json

def generation_active(page):
    # Unknown UI state is not evidence of completion.
    try:
        return page.evaluate('''() => {
            const visible = e => !!(e.getClientRects().length);
            const controls = [...document.querySelectorAll('button,[role="button"]')];
            return controls.some(e => visible(e) && !e.disabled &&
                /stop.*(generat|response)|dừng.*(tạo|phản hồi)|^stop$|^dừng$/i.test(
                    (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '') + ' ' + (e.innerText || '')));
        }''')
    except Exception:
        return None

def missing_packets(text, count):
    text = (text or '').strip()
    if text.startswith('```'):
        text = '\n'.join(text.splitlines()[1:-1])
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError('Không nhận được danh sách lượt thiếu hợp lệ')
    values = data.get('missing_parts') if isinstance(data, dict) else None
    if not isinstance(values, list) or any(type(n) is not int or not 1 <= n <= count for n in values) or len(set(values)) != len(values):
        raise ValueError('Danh sách lượt thiếu không hợp lệ')
    return values

def response_complete(text):
    """Structured analysis replies must be whole, even while UI is idle."""
    text = (text or '').strip()
    if text == 'LOI_THIEU_DU_LIEU':
        return True
    if 'LOI_THIEU_DU_LIEU' in text:
        return False
    decoder = json.JSONDecoder()
    for i, c in enumerate(text):
        if c != '{': continue
        try:
            value, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(value, dict) and ('rules' in value or 'missing_parts' in value):
            return True
    return False
