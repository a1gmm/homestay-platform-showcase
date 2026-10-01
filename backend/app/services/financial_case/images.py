"""Bounded local OCR. Image text is untrusted evidence, never executable instructions."""
from hashlib import sha256
import os
import shutil
import subprocess
import tempfile


def parse_image(data: bytes, filename: str) -> dict:
    # Decode before invoking an external parser, reject huge/deceptive containers.
    from PIL import Image, ImageOps, ImageStat, ImageDraw
    from io import BytesIO
    try:
        with Image.open(BytesIO(data)) as im:
            if im.format not in {'PNG', 'JPEG'} or im.width * im.height > 25_000_000:
                raise ValueError('图片仅支持不超过 2500 万像素的 PNG / JPEG')
            im.verify()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('无法读取这张图片，请使用完整的 PNG / JPEG 原件') from exc
    result = dict(kind='image', facts=[], summaries=[], templates=[], issues=[])
    executable = shutil.which('tesseract')
    if not executable:
        result['issues'].append(dict(code='ocr_unavailable', message='原图已保存，当前环境未安装中文识别服务。请补充图片里的文字、金额和所属月份，不能据此直接记账。'))
        return result
    with tempfile.TemporaryDirectory(prefix='case-ocr-') as folder:
        path = os.path.join(folder, 'source.png')
        with Image.open(BytesIO(data)) as original:
            gray=ImageOps.grayscale(original)
            if ImageStat.Stat(gray).mean[0]<100:
                gray=ImageOps.invert(gray)
            # Dense grid lines prevent Tesseract from segmenting small Excel cells.
            binary=gray.point(lambda value: 0 if value<110 else 255)
            width,height=gray.size
            grid_rows=[y for y in range(height) if binary.crop((0,y,width,y+1)).histogram()[0]>width*.6]
            grid_cols=[x for x in range(width) if binary.crop((x,0,x+1,height)).histogram()[0]>height*.6]
            table=len(grid_rows)>=4 and len(grid_cols)>=3
            if table:
                draw=ImageDraw.Draw(gray)
                for y in grid_rows: draw.line((0,y,width,y),fill=255)
                for x in grid_cols: draw.line((x,0,x,height),fill=255)
                gray=gray.point(lambda value: 0 if value<140 else 255)
            scale=min(3.0,3000/max(gray.size))
            if scale>1:
                gray=gray.resize((int(gray.width*scale),int(gray.height*scale)),Image.Resampling.LANCZOS)
            ImageOps.expand(ImageOps.autocontrast(gray),border=20,fill='white').save(path)
        try:
            completed = subprocess.run([executable, path, 'stdout', '-l', 'chi_sim' if table else 'chi_sim+eng', '--psm', '6' if table else '11'],
                                       capture_output=True, timeout=40, check=False)
        except (subprocess.TimeoutExpired, OSError):
            result['issues'].append(dict(code='ocr_unavailable',message='原图已保存，文字识别超时或暂不可用，请补充文字说明后继续核对。'))
            return result
    if completed.returncode:
        result['issues'].append(dict(code='ocr_failed', message='图片文字未能可靠读取，请提供更清晰的原图或文字说明。'))
        return result
    text = completed.stdout.decode('utf-8', errors='replace').strip()[:16000]
    if len(text)<20:
        result['issues'].append(dict(code='ocr_low_coverage',message='图片文字过少，识别可能不完整；请核对原图或补充文字。'))
    result['facts'].append(dict(key=sha256(data).hexdigest(), kind='image_text', text=text,
                               sheet='图片', row1based=1, amount=None, business_month=None))
    from .image_facts import extract_image_expense_evidence
    extracted = extract_image_expense_evidence(text, sha256(data).hexdigest())
    for field in ('facts', 'summaries', 'issues'):
        result[field].extend(extracted[field])
    result['issues'].append(dict(code='image_requires_verification', message='已提取图片文字；金额、费用月份、承担方及与表格的对应关系仍需核实。'))
    return result
