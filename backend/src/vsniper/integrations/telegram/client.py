from __future__ import annotations

from typing import Any

import httpx

from vsniper.core.config import get_settings


class TelegramDeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class TelegramConfigurationError(TelegramDeliveryError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class TelegramClient:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        base_url: str = "https://api.telegram.org",
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self.bot_token = bot_token or settings.telegram_bot_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.bot_token or self.bot_token == "put-your-telegram-bot-token-here":
            raise TelegramConfigurationError("Telegram bot token is missing. Add TELEGRAM_BOT_TOKEN to the environment.")
        if not chat_id or chat_id == "put-your-telegram-chat-id-here":
            raise TelegramConfigurationError("Telegram chat id is missing. Add TELEGRAM_CHAT_ID to the environment.")
        if not text.strip():
            raise TelegramDeliveryError("Telegram message body is empty.", retryable=False)

        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        return self._post_method("sendMessage", payload=payload)

    def answer_callback_query(self, *, callback_query_id: str, text: str | None = None) -> dict[str, Any]:
        if not self.bot_token or self.bot_token == "put-your-telegram-bot-token-here":
            raise TelegramConfigurationError("Telegram bot token is missing. Add TELEGRAM_BOT_TOKEN to the environment.")
        if not callback_query_id.strip():
            raise TelegramDeliveryError("Telegram callback query id is empty.", retryable=False)

        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text and text.strip():
            payload["text"] = text.strip()[:200]

        return self._post_method("answerCallbackQuery", payload=payload)

    def edit_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.bot_token or self.bot_token == "put-your-telegram-bot-token-here":
            raise TelegramConfigurationError("Telegram bot token is missing. Add TELEGRAM_BOT_TOKEN to the environment.")
        if not text.strip():
            raise TelegramDeliveryError("Telegram edited message body is empty.", retryable=False)

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        return self._post_method("editMessageText", payload=payload)

    def get_webhook_info(self) -> dict[str, Any]:
        if not self.bot_token or self.bot_token == "put-your-telegram-bot-token-here":
            raise TelegramConfigurationError("Telegram bot token is missing. Add TELEGRAM_BOT_TOKEN to the environment.")

        return self._request_method("getWebhookInfo", method="GET")

    def set_webhook(
        self,
        *,
        url: str,
        secret_token: str | None = None,
        allowed_updates: list[str] | None = None,
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        if not self.bot_token or self.bot_token == "put-your-telegram-bot-token-here":
            raise TelegramConfigurationError("Telegram bot token is missing. Add TELEGRAM_BOT_TOKEN to the environment.")
        if not url.strip():
            raise TelegramDeliveryError("Telegram webhook URL is empty.", retryable=False)

        payload: dict[str, Any] = {
            "url": url.strip(),
            "allowed_updates": allowed_updates or ["callback_query"],
            "drop_pending_updates": drop_pending_updates,
        }
        if secret_token and secret_token.strip():
            payload["secret_token"] = secret_token.strip()

        return self._post_method("setWebhook", payload=payload)

    def _post_method(self, method: str, *, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_method(method, payload=payload, method="POST")

    def _request_method(
        self,
        telegram_method: str,
        *,
        payload: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> dict[str, Any]:
        response: httpx.Response
        try:
            if self._client is not None:
                response = self._client.request(method, self._method_url(telegram_method), json=payload)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(method, self._method_url(telegram_method), json=payload)
        except httpx.TimeoutException as exc:
            raise TelegramDeliveryError(f"Telegram {telegram_method} timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise TelegramDeliveryError(f"Telegram {telegram_method} failed: {self._redact(str(exc))}", retryable=True) from exc

        data = self._parse_json(response)
        if response.is_success and data.get("ok") is True:
            result = data.get("result")
            if isinstance(result, dict):
                return result
            return {"result": result}

        description = data.get("description") if isinstance(data, dict) else None
        error_message = description or response.text or f"Telegram {telegram_method} failed with an unknown error."
        raise TelegramDeliveryError(
            error_message,
            retryable=self._is_retryable_status(response.status_code),
            status_code=response.status_code,
        )

    def _method_url(self, method: str) -> str:
        return f"{self.base_url}/bot{self.bot_token}/{method}"

    def _redact(self, text: str) -> str:
        if self.bot_token:
            text = text.replace(self.bot_token, "<redacted>")
        return text

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 409, 425, 429} or status_code >= 500
