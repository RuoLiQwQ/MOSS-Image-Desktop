import math

RATIOS = ("1:8", "1:4", "1:3", "1:2", "9:16", "2:3", "3:4", "1:1", "4:3", "3:2", "16:9", "2:1", "3:1", "4:1", "8:1")
PIXELS = (262144, 1048576, 1572864, 2097152, 4194304)


def pixel_label(pixels):
    return f"{math.isqrt(pixels)}²" if pixels < 1048576 else f"{pixels / 1048576:g}M"


def ratio_value(text):
    w, h = map(float, text.split(":"))
    return w / h


def fit_size(pixels, ratio):
    ratio = ratio_value(ratio)
    width, height = math.sqrt(pixels * ratio), math.sqrt(pixels / ratio)
    return tuple(max(16, math.floor(edge / 16 + 0.5) * 16) for edge in (width, height))


def size_selection(width, height):
    if not width or not height or width <= 0 or height <= 0:
        return 0, "custom"
    for pixels in PIXELS:
        for ratio in RATIOS:
            if fit_size(pixels, ratio) == (width, height):
                return pixels, ratio
    ratio = next(
        (r for r in RATIOS if abs(ratio_value(r) - width / height) < 1e-5),
        "custom",
    )
    return 0, ratio
