import unittest
from pathlib import Path

from codex_switcher.login import PlaywrightDriver


class _FakeNode:
    def __init__(self, visible: bool):
        self._visible = visible

    def is_visible(self) -> bool:
        return self._visible


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count

    def count(self) -> int:
        return self._count

    def nth(self, index: int):
        return _FakeNode(index < self._count)


class _FakePage:
    def __init__(self, url: str, body: str, inputs: int = 0):
        self.url = url
        self._body = body
        self._inputs = inputs

    def evaluate(self, script, arg=None):
        return self._body

    def locator(self, selector):
        return _FakeLocator(self._inputs)


DEVICE_FORM_BODY = (
    "使用你的设备代码以授予对 Codex CLI 的访问权限\n输入终端上显示的 9 位代码\n取消\n继续"
)
CALLBACK_URL = "https://auth.openai.com/deviceauth/callback?code=ac.2.test&state=abc"


class DeviceFlowDetectionTests(unittest.TestCase):
    def setUp(self):
        self.driver = PlaywrightDriver(Path("."))

    def test_callback_url_with_empty_code_form_is_not_done(self):
        page = _FakePage(CALLBACK_URL, DEVICE_FORM_BODY, inputs=9)
        self.assertFalse(self.driver._device_flow_done(page, submitted=False))
        self.assertFalse(self.driver._device_flow_done(page, submitted=True))

    def test_success_banner_after_submit_is_done(self):
        page = _FakePage(
            CALLBACK_URL,
            "已登录 Codex\n你现在可以关闭此页面\nChatGPT 的使用条款",
            inputs=0,
        )
        self.assertTrue(self.driver._device_flow_done(page, submitted=True))

    def test_success_banner_before_submit_is_not_done(self):
        page = _FakePage(CALLBACK_URL, "已登录 Codex\n你现在可以关闭此页面", inputs=0)
        self.assertFalse(self.driver._device_flow_done(page, submitted=False))

    def test_blank_page_is_not_done(self):
        page = _FakePage(CALLBACK_URL, "", inputs=0)
        self.assertFalse(self.driver._device_flow_done(page, submitted=True))

    def test_redirect_to_chatgpt_after_submit_is_done(self):
        page = _FakePage("https://chatgpt.com/", "今天想聊点什么", inputs=0)
        self.assertTrue(self.driver._device_flow_done(page, submitted=True))

    def test_choose_account_page_is_not_done(self):
        page = _FakePage(
            "https://auth.openai.com/choose-an-account",
            "欢迎回来\n选择一个账户以继续前往 Codex\n选择账户",
            inputs=0,
        )
        self.assertFalse(self.driver._device_flow_done(page, submitted=True))


if __name__ == "__main__":
    unittest.main()
