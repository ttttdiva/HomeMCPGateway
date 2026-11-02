"""Test-only Jev stdio server with a synthetic, non-Edge browser session."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
from types import SimpleNamespace

from home_mcp_gateway import browser_agent, browser_qa, jev_browser_server


SESSION_ID = "utf8-fixture-session"
TAB_HANDLE = "edge_0123456789abcdef01234567"
BASE_URL = "http://127.0.0.1:3002/utf8-fixture"
CAPTURE_DIR = Path(os.environ["HOME_MCP_UTF8_CAPTURE_DIR"])


class FixtureLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FixtureElement:
    async def fill(self, value, timeout=None):
        self.was_filled = True

    async def dispose(self):
        return None


class FixtureHandle:
    def __init__(self):
        self.element = FixtureElement()

    def as_element(self):
        return self.element


class FixtureFrame:
    async def evaluate(self, _script, _options):
        return {"candidates": {"fixture-target": {
            "tag": "textarea", "type": "", "role": "", "label": "Message",
            "text": "", "name": "", "placeholder": "Message", "title": "",
        }}}

    async def evaluate_handle(self, _script, _local_id):
        return FixtureHandle()


class FixtureLocator:
    async def inner_text(self, timeout=None):
        return ""

    async def fill(self, _value, timeout=None):
        self.was_filled = True


class FixturePage:
    def __init__(self):
        self.url = BASE_URL
        self.closed = False
        frame = FixtureFrame()
        self.frames = [frame]
        self.main_frame = frame

    def is_closed(self):
        return self.closed

    async def title(self):
        return "UTF-8 Fixture"

    def locator(self, _selector):
        return FixtureLocator()


class FixtureDecisionClient:
    model = "utf8-fixture"
    input_tokens = 0
    output_tokens = 0
    attempts = 0

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def choose(self, *, instruction, candidates, **_kwargs):
        self.__class__.attempts += 1
        selected = next(iter(candidates))
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        (CAPTURE_DIR / "received-instruction.json").write_text(
            json.dumps({"instruction": instruction}, ensure_ascii=False), encoding="utf-8"
        )
        return SimpleNamespace(
            choice=selected,
            confidence=0.99,
            probabilities={selected: 0.99, "none": 0.01},
            model=self.model,
        )


async def _fixture_close_all():
    browser_qa._SESSIONS.pop(SESSION_ID, None)


def main():
    page = FixturePage()
    browser_qa._SESSIONS[SESSION_ID] = {
        "attached": True,
        "page": page,
        "context": SimpleNamespace(pages=[page]),
        "lock": FixtureLock(),
        "selected_tab_handle": TAB_HANDLE,
        "owned_tab_handles": {TAB_HANDLE},
        "owned_tab_pages": {TAB_HANDLE: page},
        "tab_pages": {TAB_HANDLE: page},
        "closed_owned_tab_handles": set(),
        "console": deque(maxlen=10),
        "errors": deque(maxlen=10),
        "observed_page": None,
        "observer_handlers": None,
    }
    browser_agent.JevDecisionClient = FixtureDecisionClient
    browser_qa.close_all = _fixture_close_all
    jev_browser_server.main()


if __name__ == "__main__":
    main()
