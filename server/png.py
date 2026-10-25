"""最小 PNG 编解码：只为「按蒙版合成」服务。

项目运行时零第三方依赖，但局部重绘要做像素级混合，绕不开读图写图。
这里只实现实际会遇到的那一小块：
  - 解码：8 位色深、非交错，灰度 / RGB / 灰度+alpha / RGBA 四种
  - 编码：8 位 RGB 或 RGBA、非交错、每行 filter 0
引擎与浏览器 canvas 输出的 PNG 都在这个范围内；遇到别的就明确报错，
而不是悄悄产出错误画面。
"""
import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


class PngError(ValueError):
    pass


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(data):
    """返回 (width, height, channels, pixels)，pixels 是逐行排列的 bytes。"""
    if not data.startswith(_SIG):
        raise PngError("不是 PNG")
    pos = 8
    width = height = None
    depth = ctype = interlace = None
    idat = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if typ == b"IHDR":
            width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body[:13])
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break

    if width is None:
        raise PngError("缺少 IHDR")
    if depth != 8:
        raise PngError("只支持 8 位色深，当前 %s" % depth)
    if interlace != 0:
        raise PngError("不支持交错 PNG")
    ch = _CHANNELS.get(ctype)
    if not ch:
        raise PngError("不支持的色彩类型 %s" % ctype)

    raw = zlib.decompress(b"".join(idat))
    stride = width * ch
    out = bytearray(stride * height)
    prev = bytearray(stride)
    src = 0
    for y in range(height):
        ftype = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        if ftype == 1:                      # Sub
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif ftype == 2:                    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                    # Average
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                    # Paeth
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                upleft = prev[i - ch] if i >= ch else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise PngError("未知的行滤波类型 %s" % ftype)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, ch, bytes(out)


def encode(width, height, channels, pixels):
    """把 RGB(3) 或 RGBA(4) 像素编码成 PNG 字节。"""
    if channels not in (3, 4):
        raise PngError("只支持 RGB / RGBA 编码")
    stride = width * channels
    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # filter: None
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(typ, body):
        return (struct.pack(">I", len(body)) + typ + body +
                struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))

    ctype = 2 if channels == 3 else 6
    ihdr = struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0)
    return (_SIG + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def to_rgb(width, height, channels, pixels):
    """统一成 RGB 三通道，便于合成。"""
    if channels == 3:
        return pixels
    out = bytearray(width * height * 3)
    if channels == 4:
        for i in range(width * height):
            out[i * 3:i * 3 + 3] = pixels[i * 4:i * 4 + 3]
    elif channels == 1:
        for i in range(width * height):
            v = pixels[i]
            out[i * 3:i * 3 + 3] = bytes((v, v, v))
    elif channels == 2:                    # 灰度 + alpha
        for i in range(width * height):
            v = pixels[i * 2]
            out[i * 3:i * 3 + 3] = bytes((v, v, v))
    else:
        raise PngError("无法转成 RGB：channels=%s" % channels)
    return bytes(out)


def _box1d(src, n, r):
    """一维 box blur（前缀和，O(n)）。"""
    pre = [0] * (n + 1)
    for i in range(n):
        pre[i + 1] = pre[i] + src[i]
    out = bytearray(n)
    for i in range(n):
        lo = i - r
        if lo < 0:
            lo = 0
        hi = i + r + 1
        if hi > n:
            hi = n
        out[i] = (pre[hi] - pre[lo]) // (hi - lo)
    return out


def blur_gray(w, h, gray, radius=3):
    """对单通道图左右上下各做一次 box blur，用来把蒙版边缘羽化。

    硬边蒙版直接合成会出现"贴了一张方块"的接缝，羽化 3 像素后就自然了。
    """
    r = max(1, int(radius))
    rows = bytearray(w * h)
    for y in range(h):
        rows[y * w:(y + 1) * w] = _box1d(gray[y * w:(y + 1) * w], w, r)
    cols = bytearray(w * h)
    col = bytearray(h)
    for x in range(w):
        for y in range(h):
            col[y] = rows[y * w + x]
        blurred = _box1d(col, h, r)
        for y in range(h):
            cols[y * w + x] = blurred[y]
    return cols


def compose_mask(base, painted, mask, feather=3):
    """按蒙版把 painted 叠到 base 上：蒙版亮处用 painted，暗处留 base。

    三个参数都是 (w, h, ch, pixels)；尺寸不一致时做最近邻缩放。
    边缘按 feather 像素羽化，避免出现方块接缝。
    """
    bw, bh, bch, bpix = base
    pw, ph, pch, ppix = painted
    mw, mh, mch, mpix = mask
    b = to_rgb(bw, bh, bch, bpix)
    p = to_rgb(pw, ph, pch, ppix)
    m = to_rgb(mw, mh, mch, mpix)

    # 蒙版先缩放到与底图同尺寸，再取一个通道当权重
    alpha = bytearray(bw * bh)
    for y in range(bh):
        my = y * mh // bh if mh != bh else y
        for x in range(bw):
            mx = x * mw // bw if mw != bw else x
            alpha[y * bw + x] = m[(my * mw + mx) * 3]
    if feather:
        alpha = blur_gray(bw, bh, alpha, feather)

    out = bytearray(b)
    for y in range(bh):
        sy = y * ph // bh if ph != bh else y
        for x in range(bw):
            a = alpha[y * bw + x]
            if not a:
                continue
            sx = x * pw // bw if pw != bw else x
            o = (y * bw + x) * 3
            si = (sy * pw + sx) * 3
            if a == 255:
                out[o:o + 3] = p[si:si + 3]
            else:
                inv = 255 - a
                out[o] = (p[si] * a + b[o] * inv) // 255
                out[o + 1] = (p[si + 1] * a + b[o + 1] * inv) // 255
                out[o + 2] = (p[si + 2] * a + b[o + 2] * inv) // 255
    return bw, bh, 3, bytes(out)
