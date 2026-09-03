from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sub2api_monitor.visual as visual  # noqa: E402

_VALID_PNG = visual._PNG_SIGNATURE + b"fake-png-payload"
_HTML = '<article id="sub2api-test">safe local report</article>'


@dataclass
class _FakeState:
    png_bytes: bytes = _VALID_PNG
    bounds: dict[str, float] | None = field(
        default_factory=lambda: {"width": 1120.0, "height": 650.0}
    )
    screenshot_error: BaseException | None = None
    write_partial_before_error: bool = False
    block_set_content: bool = False
    block_context_close: bool = False
    block_browser_close: bool = False
    block_manager_exit: bool = False
    set_content_error: BaseException | None = None
    launch_calls: int = 0
    new_context_calls: int = 0
    new_context_kwargs: dict[str, Any] = field(default_factory=dict)
    route_pattern: str = ""
    route_calls: int = 0
    route_aborts: int = 0
    route_continues: int = 0
    new_page_calls: int = 0
    set_content_calls: int = 0
    set_content_cancelled: bool = False
    goto_calls: int = 0
    locator_selectors: list[str] = field(default_factory=list)
    screenshot_calls: int = 0
    screenshot_paths: list[Path] = field(default_factory=list)
    context_close_calls: int = 0
    browser_close_calls: int = 0
    manager_enter_calls: int = 0
    manager_exit_calls: int = 0


class _FakeRoute:
    def __init__(self, state: _FakeState) -> None:
        self._state = state

    async def abort(self) -> None:
        self._state.route_aborts += 1

    async def continue_(self) -> None:
        self._state.route_continues += 1
        raise AssertionError("visual renderer must abort every browser request")


class _FakeLocator:
    def __init__(self, state: _FakeState) -> None:
        self._state = state

    async def bounding_box(self) -> dict[str, float] | None:
        return self._state.bounds

    async def screenshot(self, *, path: str, animations: str) -> None:
        assert animations == "disabled"
        self._state.screenshot_calls += 1
        output = Path(path)
        self._state.screenshot_paths.append(output)
        if self._state.write_partial_before_error:
            output.write_bytes(b"partial")
        if self._state.screenshot_error is not None:
            raise self._state.screenshot_error
        output.write_bytes(self._state.png_bytes)


class _FakePage:
    def __init__(self, state: _FakeState, context: _FakeContext) -> None:
        self._state = state
        self._context = context

    async def set_content(self, html_text: str, *, wait_until: str) -> None:
        assert html_text == _HTML
        assert wait_until == "load"
        self._state.set_content_calls += 1
        if self._context.route_handler is not None:
            self._state.route_calls += 1
            await self._context.route_handler(_FakeRoute(self._state))
        if self._state.set_content_error is not None:
            raise self._state.set_content_error
        if self._state.block_set_content:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._state.set_content_cancelled = True
                raise

    async def goto(self, *_args: Any, **_kwargs: Any) -> None:
        self._state.goto_calls += 1
        raise AssertionError("local visual reports must use set_content, never goto")

    def locator(self, selector: str) -> _FakeLocator:
        self._state.locator_selectors.append(selector)
        return _FakeLocator(self._state)


class _FakeContext:
    def __init__(self, state: _FakeState) -> None:
        self._state = state
        self.route_handler: Any = None

    async def route(self, pattern: str, handler: Any) -> None:
        self._state.route_pattern = pattern
        self.route_handler = handler

    async def new_page(self) -> _FakePage:
        self._state.new_page_calls += 1
        return _FakePage(self._state, self)

    async def close(self) -> None:
        self._state.context_close_calls += 1
        if self._state.block_context_close:
            await asyncio.Event().wait()


class _FakeBrowser:
    def __init__(self, state: _FakeState) -> None:
        self._state = state

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        self._state.new_context_calls += 1
        self._state.new_context_kwargs = kwargs
        return _FakeContext(self._state)

    async def close(self) -> None:
        self._state.browser_close_calls += 1
        if self._state.block_browser_close:
            await asyncio.Event().wait()


class _FakeChromium:
    def __init__(self, state: _FakeState) -> None:
        self._state = state

    async def launch(self, *, headless: bool) -> _FakeBrowser:
        assert headless is True
        self._state.launch_calls += 1
        return _FakeBrowser(self._state)


class _FakePlaywrightManager:
    def __init__(self, state: _FakeState) -> None:
        self._state = state

    async def __aenter__(self) -> Any:
        self._state.manager_enter_calls += 1
        return types.SimpleNamespace(chromium=_FakeChromium(self._state))

    async def __aexit__(self, *_args: Any) -> None:
        self._state.manager_exit_calls += 1
        if self._state.block_manager_exit:
            await asyncio.Event().wait()


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    state: _FakeState,
) -> None:
    package = types.ModuleType("playwright")
    package.__path__ = []  # type: ignore[attr-defined]
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: _FakePlaywrightManager(state)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)


async def _render(tmp_path: Path) -> str | None:
    return await visual._render_html(
        _HTML,
        selector="#sub2api-test",
        artifact_dir=tmp_path,
        filename_prefix="sub2api_test",
    )


def _assert_resources_closed(state: _FakeState) -> None:
    assert state.manager_enter_calls == 1
    assert state.manager_exit_calls == 1
    assert state.context_close_calls == 1
    assert state.browser_close_calls == 1


def _assert_no_temporary_artifacts(root: Path) -> None:
    assert list(root.glob("sub2api_*.tmp.png")) == []


@pytest.mark.asyncio
async def test_render_success_is_offline_hardened_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeState()
    _install_fake_playwright(monkeypatch, state)

    rendered = await _render(tmp_path)

    assert rendered is not None
    output = Path(rendered)
    assert output.is_absolute()
    assert output.parent == tmp_path.resolve()
    assert output.read_bytes() == _VALID_PNG
    assert state.new_context_kwargs == {
        "viewport": {"width": 1220, "height": 1000},
        "device_scale_factor": 1,
        "java_script_enabled": False,
        "service_workers": "block",
    }
    assert state.route_pattern == "**/*"
    assert state.route_calls == 1
    assert state.route_aborts == 1
    assert state.route_continues == 0
    assert state.set_content_calls == 1
    assert state.goto_calls == 0
    assert state.locator_selectors == ["#sub2api-test"]
    assert state.screenshot_calls == 1
    _assert_resources_closed(state)
    _assert_no_temporary_artifacts(tmp_path)


@pytest.mark.asyncio
async def test_render_exception_removes_partial_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeState(
        screenshot_error=RuntimeError("fake screenshot failure"),
        write_partial_before_error=True,
    )
    _install_fake_playwright(monkeypatch, state)

    assert await _render(tmp_path) is None

    _assert_resources_closed(state)
    _assert_no_temporary_artifacts(tmp_path)
    assert list(tmp_path.glob("sub2api_test_*.png")) == []


@pytest.mark.asyncio
async def test_render_cancellation_removes_partial_closes_and_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeState(
        screenshot_error=asyncio.CancelledError(),
        write_partial_before_error=True,
    )
    _install_fake_playwright(monkeypatch, state)

    with pytest.raises(asyncio.CancelledError):
        await _render(tmp_path)

    _assert_resources_closed(state)
    _assert_no_temporary_artifacts(tmp_path)
    assert list(tmp_path.glob("sub2api_test_*.png")) == []


@pytest.mark.asyncio
async def test_render_timeout_cancels_work_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeState(block_set_content=True)
    _install_fake_playwright(monkeypatch, state)
    monkeypatch.setattr(visual, "_RENDER_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(visual, "_RENDER_SLOT_TIMEOUT_SECONDS", 0.01)

    assert await _render(tmp_path) is None

    assert state.set_content_cancelled is True
    assert state.screenshot_calls == 0
    _assert_resources_closed(state)
    _assert_no_temporary_artifacts(tmp_path)


@pytest.mark.asyncio
async def test_hanging_playwright_cleanup_is_independently_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeState(
        block_context_close=True,
        block_browser_close=True,
        block_manager_exit=True,
    )
    _install_fake_playwright(monkeypatch, state)
    monkeypatch.setattr(visual, "_CLEANUP_TIMEOUT_SECONDS", 0.01)

    rendered = await asyncio.wait_for(_render(tmp_path), timeout=0.2)

    assert rendered is not None
    _assert_resources_closed(state)
    _assert_no_temporary_artifacts(tmp_path)


@pytest.mark.asyncio
async def test_render_rejects_oversized_html_before_browser_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeState()
    _install_fake_playwright(monkeypatch, state)
    monkeypatch.setattr(visual, "_MAX_HTML_BYTES", 8)

    assert await _render(tmp_path) is None
    assert state.launch_calls == 0
    assert state.manager_enter_calls == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_render_rejects_excessive_height_without_screenshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeState(bounds={"width": 1120.0, "height": 101.0})
    _install_fake_playwright(monkeypatch, state)
    monkeypatch.setattr(visual, "_MAX_SCREENSHOT_HEIGHT", 100)

    assert await _render(tmp_path) is None

    assert state.screenshot_calls == 0
    _assert_resources_closed(state)
    _assert_no_temporary_artifacts(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "max_bytes"),
    [
        (b"not-a-png", 1024),
        (_VALID_PNG, 8),
    ],
    ids=["bad-signature", "too-large"],
)
async def test_render_rejects_invalid_png_and_removes_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    max_bytes: int,
) -> None:
    state = _FakeState(png_bytes=payload)
    _install_fake_playwright(monkeypatch, state)
    monkeypatch.setattr(visual, "_MAX_SCREENSHOT_BYTES", max_bytes)

    assert await _render(tmp_path) is None

    _assert_resources_closed(state)
    _assert_no_temporary_artifacts(tmp_path)
    assert list(tmp_path.glob("sub2api_test_*.png")) == []


def test_validated_artifact_rejects_relative_and_external_paths(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    good = root / "sub2api_good.png"
    good.write_bytes(_VALID_PNG)
    outside = tmp_path / "outside.png"
    outside.write_bytes(_VALID_PNG)

    assert visual.validated_artifact_image(root, good) == str(good.resolve())
    assert visual.validated_artifact_image(root, good.name) == ""
    assert visual.validated_artifact_image(root, outside) == ""
    assert (
        visual.validated_artifact_image(root, "https://example.invalid/card.png") == ""
    )


@pytest.mark.asyncio
async def test_prune_removes_tmp_files_without_touching_unrelated_files(
    tmp_path: Path,
) -> None:
    temporary = tmp_path / "sub2api_orphan.tmp.png"
    temporary.write_bytes(b"partial")
    generated = tmp_path / "sub2api_keep.png"
    generated.write_bytes(_VALID_PNG)
    unrelated = tmp_path / "keep.png"
    unrelated.write_bytes(b"unrelated")

    await visual.prune_artifacts(tmp_path)

    assert not temporary.exists()
    assert generated.read_bytes() == _VALID_PNG
    assert unrelated.read_bytes() == b"unrelated"


@pytest.mark.asyncio
async def test_symlink_artifacts_and_artifact_roots_are_rejected_without_deleting_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_VALID_PNG)
    linked_png = root / "sub2api_link.png"
    linked_tmp = root / "sub2api_link.tmp.png"
    linked_root = tmp_path / "linked-root"
    try:
        linked_png.symlink_to(outside)
        linked_tmp.symlink_to(outside)
        linked_root.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    assert visual.validated_artifact_image(root, linked_png) == ""
    with pytest.raises(ValueError, match="artifact 目录无效"):
        visual._artifact_root(linked_root)

    await visual.prune_artifacts(root)

    assert not linked_png.exists()
    assert not linked_tmp.exists()
    assert outside.read_bytes() == _VALID_PNG
