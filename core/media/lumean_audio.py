"""Озвучка через Lumean (https://api.lumean.app/docs/llm.md).

Голос и движок (ElevenLabs, Cartesia, HeyGen) задаются шаблоном в кабинете Lumean, его id — template_id в config.yaml.
Озвучка — это заказ: создать (POST /orders) → дождаться готовности (GET /orders/{id}) → получить ссылку на файл
(POST /storage/url) → скачать. Заказ стоит денег, поэтому после его создания сбои связи повторяются здесь же,
а не новым заказом.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import PurePosixPath
from typing import Any

from core.media.base import AudioProvider, Media, MediaError

BASE_URL = "https://api.lumean.app/api/public"
READY = ("completed", "result_delivered")
CLOSED = ("failed", "compensated", "cancelled")
# Отказы, которые не исправит ни повтор, ни другой кадр: дальше не тратим запросы.
FATAL_REASONS = (
    "endpoint_not_found", "ip_banned", "insufficient_balance", "insufficient_allowance", "payg_topup_required",
    "no_subscription", "entitlement_missing", "template_access_denied", "template_model_outdated",
    "service_not_found", "limit_exceeded", "token_quota_exceeded", "policy_cooldown_active",
)

log = logging.getLogger(__name__)


class LumeanAudio(AudioProvider):
    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.key = self.api_key()
        self.base_url = config.get("base_url", BASE_URL).rstrip("/")
        self.template_id = str(config.get("template_id") or "").strip()
        if not self.template_id:
            raise MediaError(
                f"[{name}] не указан template_id: создайте в кабинете Lumean шаблон озвучки с нужным голосом "
                f"и впишите его id в config.yaml → media → providers → {name} → template_id",
                fatal=True,
            )

    @property
    def model(self) -> str:
        return self.template_id

    def speak(self, text: str) -> Media:
        body: dict[str, Any] = {"template_id": self.template_id, "input_text": text}
        if self.config.get("audio_format"):
            body["config_override"] = {"output_settings": {"audio_format": self.config["audio_format"]}}
        order = self._call("POST", "/orders", body)
        result = self._wait(order["id"])
        path = result["files"][0]
        url = self._patient("POST", "/storage/url", {"path": path})["url"]
        return Media(data=self._download(url), extension=PurePosixPath(path).suffix or ".mp3", model=self.model)

    def _wait(self, order_id: str) -> dict[str, Any]:
        """Дождаться готовности заказа. Новый заказ при сбое не создаётся — первый уже оплачен."""
        deadline = time.monotonic() + self.config.get("timeout", 900)
        while True:
            order = self._patient("GET", f"/orders/{order_id}")
            status = order.get("status")
            result = order.get("result") or {}
            if status in READY and result.get("files"):
                return result
            items = order.get("items") or []
            if status in CLOSED or (status == "partially_completed" and not any(item.get("status") in ("pending", "processing") for item in items)):
                self._fail(order_id, status, order, items)
            if time.monotonic() > deadline:
                raise MediaError(f"[{self.name}] заказ {order_id} не готов за отведённое время (статус {status}) — проверьте его в кабинете", retry=False)
            time.sleep(self.config.get("poll_seconds", 3))

    def _fail(self, order_id: str, status: str, order: dict[str, Any], items: list[dict[str, Any]]) -> None:
        if status == "partially_completed":
            try:  # незавершённая часть возвращается на баланс
                self._call("POST", f"/orders/{order_id}/cancel", {})
            except MediaError as e:
                log.warning("[%s] не удалось отменить заказ %s: %s", self.name, order_id, e)
        flagged = any(item.get("status") == "policy_flagged" for item in items)
        reasons = [item["failure_reason"] for item in items if item.get("failure_reason")]
        reason = "; ".join(reasons[:2]) or (order.get("result") or {}).get("user_message") or "причину смотрите в кабинете"
        if flagged:
            raise MediaError(f"[{self.name}] текст кадра отклонён контентной политикой: {reason}", retry=False)
        raise MediaError(f"[{self.name}] заказ {order_id} не выполнен ({status}): {reason}")

    def _patient(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Запрос к уже оплаченному заказу: временные сбои пережидаются."""
        attempts = self.config.get("attempts", 5)
        for attempt in range(1, attempts + 1):
            try:
                return self._call(method, path, body)
            except MediaError as e:
                if not e.retry or attempt == attempts:
                    raise
                time.sleep(self.config.get("poll_seconds", 3) * attempt)
        raise AssertionError

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-API-KEY": self.key, "Accept": "application/json", "User-Agent": "ai-factory"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise self._error(e) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise MediaError(f"[{self.name}] нет соединения с API: {e}") from e
        except ValueError as e:
            raise MediaError(f"[{self.name}] непонятный ответ API: {e}") from e
        return payload.get("data") or {}

    def _error(self, error: urllib.error.HTTPError) -> MediaError:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except ValueError:
            body = {}
        code, reason = error.code, body.get("reason") or body.get("error_code") or ""
        detail = body.get("message") or error.reason
        if body.get("errors"):
            detail = f"{detail}: {json.dumps(body['errors'], ensure_ascii=False)}"
        prefix = f"[{self.name}] {detail}" + (f" ({reason})" if reason else "")
        if code == 401:
            return MediaError(f"[{self.name}] неверный API-ключ", fatal=True)
        if code == 403:
            return MediaError(f"{prefix} — проверьте права ключа: нужны orders.read, orders.write и orders.download", fatal=True)
        if reason in FATAL_REASONS or code in (402, 404):
            return MediaError(prefix, fatal=True)
        if code == 429:  # ждём столько, сколько просит сервер; concurrency_limit_exceeded подсказки не несёт
            wait = error.headers.get("Retry-After") if error.headers else None
            time.sleep(min(float(wait), 60) if wait and wait.replace(".", "", 1).isdigit() else 15)
            return MediaError(prefix)
        if code in (400, 405, 409, 422):
            return MediaError(prefix, retry=False)
        return MediaError(prefix)

    def _download(self, url: str) -> bytes:
        attempts = self.config.get("attempts", 5)
        for attempt in range(1, attempts + 1):
            try:  # ссылка временная и подписанная: ключ ей не нужен
                request = urllib.request.Request(url, headers={"User-Agent": "ai-factory"})
                with urllib.request.urlopen(request, timeout=300) as response:
                    data = response.read()
                if data:
                    return data
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == attempts:
                    raise MediaError(f"[{self.name}] не удалось скачать файл: {e}", retry=False) from e
            time.sleep(self.config.get("poll_seconds", 3) * attempt)
        raise MediaError(f"[{self.name}] скачан пустой файл", retry=False)
