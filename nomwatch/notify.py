"""
Notification backends. v1 targets free, account-light push services -
no custom iOS app or Apple Developer account needed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import requests

from .config import NotifyConfig


class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, message: str) -> bool:
        raise NotImplementedError


class NtfyNotifier(Notifier):
    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.url = f"{server.rstrip('/')}/{topic}"

    def send(self, title: str, message: str) -> bool:
        # A transient network failure here must NOT raise: send() is called
        # from the long-running monitoring loop, and an uncaught
        # requests.ConnectionError would kill the whole loop over one
        # missed push.
        try:
            resp = requests.post(
                self.url,
                data=message.encode("utf-8"),
                headers={"Title": title},
                timeout=10,
            )
        except requests.RequestException:
            return False
        return resp.ok


class PushoverNotifier(Notifier):
    def __init__(self, user_key: str, app_token: str):
        self.user_key = user_key
        self.app_token = app_token

    def send(self, title: str, message: str) -> bool:
        try:
            resp = requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": self.app_token,
                    "user": self.user_key,
                    "title": title,
                    "message": message,
                },
                timeout=10,
            )
        except requests.RequestException:
            return False
        return resp.ok


def build_notifier(cfg: NotifyConfig) -> Notifier | None:
    if cfg.provider == "ntfy" and cfg.ntfy_topic:
        return NtfyNotifier(cfg.ntfy_topic)
    if cfg.provider == "pushover" and cfg.pushover_user_key and cfg.pushover_app_token:
        return PushoverNotifier(cfg.pushover_user_key, cfg.pushover_app_token)
    return None
