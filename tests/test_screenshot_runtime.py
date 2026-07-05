from src.services.screenshot_runtime import _dismiss_cookie_banner_once


class _FakeLocator:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.clicked = False

    @property
    def first(self):
        return self

    def click(self, *, timeout: int):
        self.clicked = True
        if self.fail:
            raise RuntimeError("not found")


class _FakePage:
    def __init__(self, *, fail: bool = False):
        self.locator_arg = ""
        self.locator_obj = _FakeLocator(fail=fail)
        self.waited = False

    def locator(self, selector: str):
        self.locator_arg = selector
        return self.locator_obj

    def wait_for_timeout(self, _timeout_ms: int):
        self.waited = True


def test_dismiss_cookie_banner_once_reuses_shared_cookie_controls():
    page = _FakePage()

    result = _dismiss_cookie_banner_once(page)

    assert result["attempted"] is True
    assert result["success"] is True
    assert "Aceptar" in page.locator_arg
    assert "Accept all" in page.locator_arg
    assert page.locator_obj.clicked is True
    assert page.waited is True


def test_dismiss_cookie_banner_once_records_failed_attempt():
    page = _FakePage(fail=True)

    result = _dismiss_cookie_banner_once(page)

    assert result["attempted"] is True
    assert result["success"] is False
    assert "not found" in result["error"]
