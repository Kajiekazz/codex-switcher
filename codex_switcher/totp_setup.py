"""Read a binding challenge locally; only confirmed seeds become login secrets."""
from __future__ import annotations

import base64
import io
import re
from urllib.parse import parse_qs, unquote, urlparse

from .accounts import normalize_totp_secret

SECURITY_URL = "https://chatgpt.com/settings/security"
BINDING_URL = SECURITY_URL + "?action=enable&factor=totp"


class TotpSetupError(Exception):
    pass


def provisioning_secret(value: str, email: str) -> str:
    """Accept the parameters our generator supports, without leaking the URI."""
    value = value.strip()
    if value.lower().startswith("otpauth://"):
        uri = urlparse(value)
        query = parse_qs(uri.query)
        if any(len(values) != 1 for values in query.values()):
            raise TotpSetupError("绑定码参数不明确，请手动设置")
        issuer = (query.get("issuer") or [""])[0].casefold()
        if issuer and issuer not in {"openai", "chatgpt", "chatgpt.com"}:
            raise TotpSetupError("二维码不属于 OpenAI，请检查绑定页面")
        if uri.netloc != "totp":
            raise TotpSetupError("二维码不是 TOTP 绑定码，请手动设置")
        label = unquote(uri.path.lstrip("/")).rsplit(":", 1)[-1].strip()
        if "@" in label and label.casefold() != email.casefold():
            raise TotpSetupError("二维码所属账号不一致，请核对浏览器账号")
        try:
            supported = ((query.get("algorithm") or ["SHA1"])[0].upper() == "SHA1"
                         and int((query.get("digits") or ["6"])[0]) == 6
                         and int((query.get("period") or ["30"])[0]) == 30)
        except ValueError:
            supported = False
        if not supported:
            raise TotpSetupError("绑定码参数不受支持，请手动设置")
    seed = normalize_totp_secret(value)
    if not re.fullmatch(r"[A-Z2-7]{16,128}", seed):
        raise TotpSetupError("未读取到有效的 TOTP 密钥，请手动设置")
    try:
        base64.b32decode(seed + "=" * ((8 - len(seed) % 8) % 8))
    except ValueError:
        raise TotpSetupError("TOTP 密钥格式无效，请手动设置") from None
    return seed


def decode_binding_image(pixels: bytes, email: str) -> str:
    try:
        from PIL import Image
        import zxingcpp
        with Image.open(io.BytesIO(pixels)) as picture:
            if picture.width * picture.height > 16_000_000:
                raise TotpSetupError("二维码图片过大，请手动输入密钥")
            codes = zxingcpp.read_barcodes(picture)
        candidates = [code.text for code in codes if code.text.lower().startswith("otpauth://")]
        if len(candidates) != 1:
            raise TotpSetupError("无法识别唯一的绑定二维码，请手动设置")
        return provisioning_secret(candidates[0], email)
    except TotpSetupError:
        raise
    except Exception:
        raise TotpSetupError("无法自动读取二维码，请在浏览器中手动设置") from None


def read_binding_secret(page, email: str, *, allow_toggle=True) -> str:
    dialog = page.get_by_role("dialog").filter(has=page.locator('input[name="verification-code"]')).first
    dialog.wait_for(state="visible", timeout=10_000)
    # Preserve just this QR in memory before switching to the textual view.
    pixels = None
    qr = dialog.locator('svg[aria-label*="二维码"], svg[aria-label*="QR"], canvas, img').first
    try:
        if qr.is_visible():
            pixels = qr.screenshot(timeout=3_000)
    except Exception:
        pass
    try:
        manual = dialog.get_by_role("button", name=re.compile(r"无法扫描|不能扫描|can.t scan|manual", re.I)).first
        if allow_toggle and manual.is_visible():
            manual.click(timeout=2_000)
        candidates = dialog.locator('a[href^="otpauth://"], input[readonly], textarea[readonly], code, pre, [class*="mono"], [class*="break-all"]')
        for index in range(min(candidates.count(), 20)):
            node = candidates.nth(index)
            if not node.is_visible():
                continue
            value = node.get_attribute("href") or node.get_attribute("value") or node.inner_text()
            try:
                return provisioning_secret(value, email)
            except TotpSetupError:
                continue
    except Exception:
        pass
    if pixels:
        return decode_binding_image(pixels, email)
    raise TotpSetupError("未读取到绑定密钥，请在浏览器中手动设置")


def factor_state(page) -> str:
    """A closed modal alone is never evidence that MFA was enabled."""
    try:
        state = page.evaluate(r"""() => {
          const label = [...document.querySelectorAll('h2,h3,span,p,div')].find(el =>
            /^(身份验证器应用|验证器应用|Authenticator app)$/i.test((el.textContent || '').trim()));
          if (!label) return 'unknown';
          let node = label.closest('button,[role="button"]') || label.parentElement;
          for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
            const text = (node.innerText || '').trim();
            if (text.length > 500) break;
            const control = node.matches('[role="switch"]') ? node : node.querySelector('[role="switch"]');
            if (control && control.getAttribute('aria-checked') === 'true') return 'enabled';
            if (control && control.getAttribute('aria-checked') === 'false') return 'disabled';
            if (/已启用|已开启|移除|删除|关闭|管理|\benabled\b|\bremove\b|\bdisable\b|\bmanage\b/i.test(text)) return 'enabled';
            if (/添加|开启|启用|\badd\b|\benable\b/i.test(text)) return 'disabled';
            if (node.matches('button,[role="button"]') || node.querySelector('button')) break;
          }
          return 'unknown';
        }""")
        return state if state in {"enabled", "disabled"} else "unknown"
    except Exception:
        return "unknown"


def factor_enabled(page) -> bool:
    return factor_state(page) == "enabled"
