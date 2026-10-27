from __future__ import annotations

import base64
import asyncio
import json
import logging
import re
import statistics
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp
from aiogram import Bot
from aiogram.types import Message

from .config import Settings


LOGGER = logging.getLogger(__name__)
CUSTOM_PROMPT_MAX_CHARS = 4000
SYSTEM_PROMPT_MAX_CHARS = 4096
AI_PROVIDERS = {"openai", "gemini", "deepseek"}
DEEPSEEK_API_URL = "https://api.deepseek.com"

# Prompts without their own answer format get RUBRIC_FORMAT appended: the model scores criteria, the code decides.
SYSTEM_PROMPTS = {
    "ru": (
        "Ты модерируешь заявки в Telegram-канал. Оцени, стоит ли публиковать присланное сообщение "
        "для читателей этого канала."
    ),
    "en": (
        "You moderate submissions for a Telegram channel. Judge whether the submitted message is worth "
        "publishing for this channel's readers."
    ),
}

RUBRIC_CRITERIA = ("fit", "quality", "clarity")
RUBRIC_CRITERION_MAX = 3
RUBRIC_MAX = RUBRIC_CRITERION_MAX * len(RUBRIC_CRITERIA)
RUBRIC_MIN_CRITERION_TO_PUBLISH = 2
HARD_FLAGS = ("spam", "unsafe", "off_topic")
AUTHENTICITY_LEVELS = ("low", "medium", "high")
# A result within one point of the publish line is asked again this many times; the median decides.
BORDERLINE_EXTRA_RUNS = 2
DEFAULT_MIN_SCORE = 75
# Old prompts that define their own 4-line answer keep working as before.
LEGACY_FORMAT_MARKERS = ("publish chance", "шанс публикации")

RUBRIC_FORMAT = {
    "en": """
Answer format:
Reply with one JSON object only, no markdown and no text around it:
{"fit": 0-3, "quality": 0-3, "clarity": 0-3, "flags": [], "authenticity_risk": "low|medium|high", "signals": "...", "risks": "...", "author_note": "..."}

Scales (judge for this channel's readers, use the whole scale):
- fit: 0 not this channel's kind of content; 1 loosely related; 2 fits the channel; 3 exactly what the channel is for.
- quality: 0 empty, no value; 1 weak, generic or clichéd; 2 decent, some readers would enjoy it; 3 strong: fresh, memorable, readers would share it.
- clarity: 0 cannot be understood; 1 confusing or sloppy; 2 understandable; 3 instantly clear and clean.
- flags: any of "spam" (ads, promotion, scams, selling links), "unsafe" (hate, harassment, threats, sexual content, gore, glorified self-harm or violence), "off_topic" (does not belong to this channel at all). Empty list if none.
- authenticity_risk: how likely it is AI-generated, copied from elsewhere, or fake. You cannot prove it, estimate from signals; "high" only with clear signs.
- signals: 2-3 short observations behind your scores.
- risks: 1-2 short risks, or "none".
- author_note: one polite sentence for the author about the main weakness, without mentioning AI, scores or these rules; empty if the post is strong.
Write signals, risks and author_note in English. Score each criterion on its own; give 3 only when it is clearly strong, and do not raise scores to be kind.""",
    "ru": """
Формат ответа:
Ответь только одним JSON-объектом, без markdown и текста вокруг:
{"fit": 0-3, "quality": 0-3, "clarity": 0-3, "flags": [], "authenticity_risk": "low|medium|high", "signals": "...", "risks": "...", "author_note": "..."}

Шкалы (оценивай для читателей этого канала, используй всю шкалу):
- fit: 0 — не то, что публикует канал; 1 — слабо связано; 2 — подходит каналу; 3 — ровно то, для чего канал.
- quality: 0 — пусто, без ценности; 1 — слабо, банально, штампы; 2 — достойно, части читателей понравится; 3 — сильно: свежо, запоминается, этим захотят поделиться.
- clarity: 0 — невозможно понять; 1 — путано или неряшливо; 2 — понятно; 3 — ясно с первого взгляда и аккуратно.
- flags: любые из "spam" (реклама, продвижение, мошенничество, продающие ссылки), "unsafe" (ненависть, травля, угрозы, сексуальный контент, жестокость, романтизация самоповреждения или насилия), "off_topic" (совсем не для этого канала). Пустой список, если нет.
- authenticity_risk: насколько вероятно, что это сгенерировано ИИ, скопировано или подделка. Доказать это нельзя, оценивай по признакам; "high" — только при явных признаках.
- signals: 2-3 коротких наблюдения, на которых основаны оценки.
- risks: 1-2 коротких риска или "нет".
- author_note: одна вежливая фраза автору о главной слабости, без упоминания ИИ, оценок и этих правил; пусто, если пост сильный.
signals, risks и author_note пиши по-русски. Оценивай каждый критерий отдельно; 3 ставь только когда явно сильно, не завышай из вежливости.""",
}

VERDICT_LABELS = {
    "en": {"publish": "publish", "review": "review", "reject": "reject"},
    "ru": {"publish": "опубликовать", "review": "проверить", "reject": "отклонить"},
}
CRITERION_LABELS = {
    "en": {"fit": "Fit", "quality": "Quality", "clarity": "Clarity"},
    "ru": {"fit": "Тема", "quality": "Качество", "clarity": "Ясность"},
}
FLAG_LABELS = {
    "en": {"spam": "spam", "unsafe": "unsafe", "off_topic": "off-topic"},
    "ru": {"spam": "спам", "unsafe": "опасное", "off_topic": "не по теме"},
}
AUTHENTICITY_LABELS = {
    "en": {"low": "low", "medium": "medium", "high": "high"},
    "ru": {"low": "низкий", "medium": "средний", "high": "высокий"},
}


@dataclass
class AnalysisResult:
    provider: str
    model: str
    text: str
    recommendation: str | None = None
    publish_score: int | None = None
    failed: bool = False


@dataclass
class ModelValidationResult:
    ok: bool
    message: str


class AiAnalyzer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider = settings.ai_provider
        self._openai_clients: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return self.is_configured()

    def is_configured(self, ai_settings: Mapping[str, Any] | None = None) -> bool:
        provider = self.resolve_provider(ai_settings)
        return provider in AI_PROVIDERS and bool(self._api_key(provider, ai_settings))

    def resolve_provider(self, ai_settings: Mapping[str, Any] | None = None) -> str:
        provider = str((ai_settings or {}).get("provider") or self.provider or "none").lower()
        return provider if provider in {"none", *AI_PROVIDERS} else "none"

    def api_key(self, provider: str, ai_settings: Mapping[str, Any] | None = None) -> str | None:
        return self._api_key(provider, ai_settings)

    async def analyze(
        self,
        bot: Bot,
        message: Message,
        ai_settings: Mapping[str, Any] | None = None,
        language: str = "ru",
    ) -> AnalysisResult | None:
        provider = self.resolve_provider(ai_settings)
        api_key = self._api_key(provider, ai_settings)
        model = self._model(provider, ai_settings)
        if provider == "none" or not api_key:
            return None

        try:
            text = _message_text(message)
            image = await self._extract_image(bot, message)
            prompt = _build_user_prompt(message, text, image, language)
            system_prompt = build_system_prompt(language, ai_settings)
            rubric_mode = not uses_legacy_format(system_prompt)

            async def ask() -> str:
                if provider == "openai":
                    answer = await self._analyze_openai(prompt, image, api_key, model, system_prompt)
                elif provider == "gemini":
                    answer = await self._analyze_gemini(prompt, image, api_key, model, system_prompt)
                else:
                    answer = await self._analyze_deepseek(prompt, image, api_key, model, system_prompt, rubric_mode)
                if not answer:
                    raise RuntimeError("AI provider returned an empty answer")
                return answer

            result = await ask()
        except Exception:
            LOGGER.exception("AI analysis failed")
            return AnalysisResult(provider=provider, model=model, text=_failure_text(language), failed=True)

        rubric = _parse_rubric(result) if rubric_mode else None
        if rubric is None:
            if rubric_mode:
                LOGGER.warning("AI answer is not a rubric JSON, reading it as text: %s", result[:200])
            recommendation, publish_score = _parse_analysis_result(result)
            return AnalysisResult(
                provider=provider,
                model=model,
                text=_trim(result, 900),
                recommendation=recommendation,
                publish_score=publish_score,
            )

        min_score = _min_score(ai_settings)
        runs = 1
        if _rubric_borderline(rubric, min_score):
            extra = await asyncio.gather(*(ask() for _ in range(BORDERLINE_EXTRA_RUNS)), return_exceptions=True)
            samples = [rubric, *(r for r in (_parse_rubric(x) for x in extra if isinstance(x, str)) if r)]
            rubric = _combine_rubrics(samples)
            runs = len(samples)
        recommendation, publish_score = rubric_decision(rubric, min_score)
        return AnalysisResult(
            provider=provider,
            model=model,
            text=_trim(_render_rubric(rubric, recommendation, publish_score, runs, language), 1200),
            recommendation=recommendation,
            publish_score=publish_score,
        )

    async def validate_model(
        self,
        provider: str,
        model: str,
        ai_settings: Mapping[str, Any] | None = None,
    ) -> ModelValidationResult:
        provider = self.resolve_provider({"provider": provider})
        api_key = self._api_key(provider, ai_settings)
        model = model.strip()
        if provider not in AI_PROVIDERS:
            return ModelValidationResult(False, "Неизвестный AI-провайдер.")
        if not api_key:
            return ModelValidationResult(False, "Сначала добавьте API-ключ.")
        if not model or any(ch.isspace() for ch in model):
            return ModelValidationResult(False, "Название модели не должно быть пустым или содержать пробелы.")

        if provider == "openai":
            return await self._validate_openai_model(api_key, model)
        if provider == "deepseek":
            return await self._validate_deepseek_model(api_key, model)
        return await self._validate_gemini_model(api_key, model)

    def _api_key(self, provider: str, ai_settings: Mapping[str, Any] | None) -> str | None:
        settings = ai_settings or {}
        if provider == "openai":
            return settings.get("openai_api_key") or self.settings.openai_api_key
        if provider == "gemini":
            return settings.get("gemini_api_key") or self.settings.gemini_api_key
        if provider == "deepseek":
            return settings.get("deepseek_api_key") or self.settings.deepseek_api_key
        return None

    def _model(self, provider: str, ai_settings: Mapping[str, Any] | None) -> str:
        settings = ai_settings or {}
        if provider == "openai":
            return str(settings.get("openai_model") or self.settings.openai_model)
        if provider == "gemini":
            return str(settings.get("gemini_model") or self.settings.gemini_model)
        if provider == "deepseek":
            return str(settings.get("deepseek_model") or self.settings.deepseek_model)
        return ""

    async def _validate_openai_model(self, api_key: str, model: str) -> ModelValidationResult:
        from openai import AsyncOpenAI

        client = self._openai_clients.get(api_key)
        if client is None:
            client = AsyncOpenAI(api_key=api_key)
            self._openai_clients[api_key] = client

        try:
            await asyncio.wait_for(client.models.retrieve(model), timeout=25)
        except Exception as exc:
            LOGGER.info("OpenAI model %s is not available: %s", model, exc.__class__.__name__)
            return ModelValidationResult(False, f"Модель {model} недоступна для этого OpenAI-ключа.")

        return ModelValidationResult(True, f"Модель {model} найдена в аккаунте и сохранена.")

    async def _validate_deepseek_model(self, api_key: str, model: str) -> ModelValidationResult:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{DEEPSEEK_API_URL}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as response:
                    if response.status >= 400:
                        LOGGER.info("DeepSeek models list failed with status %s", response.status)
                        return ModelValidationResult(False, "Не смог проверить DeepSeek-модели этим ключом.")
                    body = await response.json()
        except Exception:
            LOGGER.info("DeepSeek model %s validation failed", model, exc_info=True)
            return ModelValidationResult(False, f"Не смог проверить модель {model}.")

        if model not in {str(item.get("id", "")) for item in body.get("data", [])}:
            return ModelValidationResult(False, f"Модель {model} недоступна для этого DeepSeek-ключа.")
        return ModelValidationResult(True, f"Модель {model} найдена в аккаунте и сохранена.")

    async def _validate_gemini_model(self, api_key: str, model: str) -> ModelValidationResult:
        normalized = model.removeprefix("models/")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": api_key},
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as response:
                    if response.status >= 400:
                        LOGGER.info("Gemini models list failed with status %s", response.status)
                        return ModelValidationResult(False, "Не смог проверить Gemini-модели этим ключом.")
                    body = await response.json()

                matched = None
                for item in body.get("models", []):
                    name = str(item.get("name", ""))
                    if name == f"models/{normalized}" or name.rsplit("/", 1)[-1] == normalized:
                        matched = item
                        break
                if matched is None:
                    return ModelValidationResult(False, f"Модель {normalized} недоступна для этого Gemini-ключа.")
                methods = set(matched.get("supportedGenerationMethods") or [])
                if methods and "generateContent" not in methods:
                    return ModelValidationResult(False, f"Модель {normalized} не поддерживает generateContent.")

                async with session.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{normalized}:generateContent",
                    params={"key": api_key},
                    json={
                        "contents": [{"role": "user", "parts": [{"text": "OK"}]}],
                        "generationConfig": {"maxOutputTokens": 8},
                    },
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as response:
                    if response.status >= 400:
                        LOGGER.info("Gemini model %s validation failed with status %s", normalized, response.status)
                        return ModelValidationResult(False, f"Модель {normalized} не прошла тест generateContent.")
        except Exception:
            LOGGER.info("Gemini model %s validation failed", normalized, exc_info=True)
            return ModelValidationResult(False, f"Не смог проверить модель {normalized}.")

        return ModelValidationResult(True, f"Модель {normalized} проверена и сохранена.")

    async def _extract_image(self, bot: Bot, message: Message) -> AiImage | None:
        file_id: str | None = None
        mime_type = "image/jpeg"
        file_size: int | None = None
        source = "image"

        if message.photo:
            photo = message.photo[-1]
            file_id = photo.file_id
            file_size = photo.file_size
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            mime_type = message.document.mime_type or mime_type
            file_size = message.document.file_size
        else:
            # Video, GIF and video notes are not sent to the model as is; their preview frame still shows a lot.
            media = message.video or message.animation or message.video_note
            thumbnail = getattr(media, "thumbnail", None) if media else None
            if thumbnail:
                file_id = thumbnail.file_id
                file_size = thumbnail.file_size
                source = "gif" if message.animation else "video"
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            return None

        if not file_id:
            return None
        if file_size and file_size > self.settings.ai_max_image_bytes:
            return None

        telegram_file = await bot.get_file(file_id)
        if telegram_file.file_size and telegram_file.file_size > self.settings.ai_max_image_bytes:
            return None
        if not telegram_file.file_path:
            return None

        stream = await bot.download_file(telegram_file.file_path)
        data = stream.read()
        if len(data) > self.settings.ai_max_image_bytes:
            return None
        return AiImage(mime_type, data, source)

    async def _analyze_openai(
        self,
        prompt: str,
        image: AiImage | None,
        api_key: str,
        model: str,
        system_prompt: str,
    ) -> str:
        from openai import AsyncOpenAI

        client = self._openai_clients.get(api_key)
        if client is None:
            client = AsyncOpenAI(api_key=api_key)
            self._openai_clients[api_key] = client

        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        if image:
            content.append({"type": "input_image", "image_url": image.data_url})

        response = await client.responses.create(
            model=model,
            instructions=system_prompt,
            input=[{"role": "user", "content": content}],
            max_output_tokens=700,
        )
        return response.output_text.strip()

    async def _analyze_gemini(
        self,
        prompt: str,
        image: AiImage | None,
        api_key: str,
        model: str,
        system_prompt: str,
    ) -> str:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )

        parts: list[dict] = [{"text": prompt}]
        if image:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": image.mime_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    }
                }
            )

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                response.raise_for_status()
                body = await response.json()

        return (
            body.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )


    async def _analyze_deepseek(
        self,
        prompt: str,
        image: AiImage | None,
        api_key: str,
        model: str,
        system_prompt: str,
        json_answer: bool = False,
    ) -> str:
        user_content: str | list[dict[str, Any]] = prompt
        if image:
            user_content = [
                {"type": "image_url", "image_url": {"url": image.data_url}},
                {"type": "text", "text": prompt},
            ]
        effort = self.settings.deepseek_reasoning_effort
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            # Reasoning tokens count towards max_tokens, so leave room for thinking before the 4-line answer.
            "max_tokens": 700 if effort == "off" else 6000,
            "thinking": {"type": "disabled" if effort == "off" else "enabled"},
        }
        if effort == "off":
            payload["temperature"] = 0.2
        else:
            payload["reasoning_effort"] = effort
        if json_answer:
            payload["response_format"] = {"type": "json_object"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DEEPSEEK_API_URL}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=80),
            ) as response:
                if response.status >= 400:
                    body_text = await response.text()
                    raise RuntimeError(f"DeepSeek HTTP {response.status}: {body_text[:300]}")
                body = await response.json()

        choice = (body.get("choices") or [{}])[0]
        return str((choice.get("message") or {}).get("content") or "").strip()


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@dataclass
class AiImage:
    mime_type: str
    data: bytes
    source: str = "image"

    @property
    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{base64.b64encode(self.data).decode('ascii')}"


def _message_text(message: Message) -> str:
    return message.text or message.caption or ""


def _build_user_prompt(message: Message, text: str, image: AiImage | None, language: str) -> str:
    has_media = bool(message.photo or message.video or message.animation or message.video_note or message.document)
    forwarded = bool(message.forward_origin)
    if language == "en":
        parts = [
            f"Telegram message type: {message.content_type}",
            "Text/caption:",
            text.strip() or "<no text>",
        ]
        if forwarded:
            parts.append("The author forwarded this message from another chat or channel.")
        if image and image.source == "image":
            parts.append("The message includes an image (attached). Analyze it too.")
        elif image:
            parts.append(
                f"The message is a {image.source}. You only see its preview frame (attached), not the full "
                "clip: judge the frame and the caption, and do not penalize what the frame cannot show."
            )
        elif has_media:
            parts.append("The message has media that you cannot see. Judge by the caption and say so in Risks.")
        return "\n".join(parts)

    parts = [
        f"Тип Telegram-сообщения: {message.content_type}",
        "Текст/подпись:",
        text.strip() or "<нет текста>",
    ]
    if forwarded:
        parts.append("Автор переслал это сообщение из другого чата или канала.")
    if image and image.source == "image":
        parts.append("К сообщению приложено изображение. Проанализируй и его тоже.")
    elif image:
        kind = "GIF" if image.source == "gif" else "видео"
        parts.append(
            f"Это {kind}. Тебе видна только его заставка (приложена), а не весь ролик: оценивай кадр и подпись "
            "и не снижай оценку за то, что по одному кадру не видно."
        )
    elif has_media:
        parts.append("В сообщении есть медиа, которое тебе не видно. Оценивай по подписи и укажи это в рисках.")
    return "\n".join(parts)


def build_system_prompt(
    language: str,
    ai_settings: Mapping[str, Any] | None = None,
    *,
    with_format: bool = True,
) -> str:
    """The prompt sent to the model; with_format=False gives the part the owner edits."""
    language = "en" if language == "en" else "ru"
    prompt = _system_prompt_override(ai_settings) or _default_prompt(language, ai_settings)
    if not with_format or uses_legacy_format(prompt):
        return prompt
    return f"{prompt}\n{RUBRIC_FORMAT[language]}"


def uses_legacy_format(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(marker in lowered for marker in LEGACY_FORMAT_MARKERS)


def _default_prompt(language: str, ai_settings: Mapping[str, Any] | None) -> str:
    base = SYSTEM_PROMPTS[language]
    custom_prompt = _custom_prompt(ai_settings)
    if not custom_prompt:
        return base
    if language == "en":
        return _trim_system_prompt(
            f"{base}\n\n"
            "Owner channel context and moderation rules:\n"
            f"{custom_prompt}\n\n"
            "Treat these owner rules as authoritative. If the submission is off-topic "
            "or violates them, lower the scores and set the matching flag."
        )
    return _trim_system_prompt(
        f"{base}\n\n"
        "Дополнительный контекст канала и правила владельца:\n"
        f"{custom_prompt}\n\n"
        "Считай эти правила приоритетными. Если сообщение не подходит тематике канала "
        "или нарушает их, снижай оценки и ставь подходящий флаг."
    )


def _system_prompt_override(ai_settings: Mapping[str, Any] | None) -> str:
    value = (ai_settings or {}).get("system_prompt")
    if not isinstance(value, str):
        return ""
    return _trim_system_prompt(value.strip())


def _custom_prompt(ai_settings: Mapping[str, Any] | None) -> str:
    value = (ai_settings or {}).get("custom_prompt")
    if not isinstance(value, str):
        return ""
    return value.strip()[:CUSTOM_PROMPT_MAX_CHARS]


def _trim_system_prompt(value: str) -> str:
    value = value.strip()
    if len(value) <= SYSTEM_PROMPT_MAX_CHARS:
        return value
    return value[:SYSTEM_PROMPT_MAX_CHARS].rstrip()


def _failure_text(language: str) -> str:
    if language == "en":
        return "AI analysis is temporarily unavailable."
    return "AI-анализ временно недоступен."


def _trim(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _parse_analysis_result(value: str) -> tuple[str | None, int | None]:
    lines = [line.strip().replace("*", "") for line in value.splitlines() if line.strip()]
    verdict_lines = [line for line in lines if re.search(r"verdict|вердикт", line, flags=re.IGNORECASE)]
    first_line = (verdict_lines or lines or [value])[0].lower()
    # The template lists all options ("publish/review/reject"); a model that echoes it has not decided.
    if re.search(r"publish\s*/\s*review|опубликовать\s*/\s*проверить", first_line):
        return None, None
    value = "\n".join(lines)
    verdict = first_line.split("|", 1)[0]

    recommendation: str | None = None
    if "отклон" in verdict or re.search(r"\breject\b", verdict):
        recommendation = "reject"
    elif "провер" in verdict or re.search(r"\breview\b", verdict):
        recommendation = "review"
    elif "опубликов" in verdict or re.search(r"\bpublish\b", verdict):
        recommendation = "publish"

    score: int | None = None
    # Only the verdict line may hold the score: other lines carry unrelated percentages (authenticity risk).
    for pattern, source in (
        (r"(?:шанс публикации|publish chance)\D{0,24}(\d{1,3})\s*%", value),
        (r"(\d{1,3})\s*%", first_line),
    ):
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match:
            score = max(0, min(100, int(match.group(1))))
            break

    return recommendation, score


def needed_total(min_score: int) -> int:
    """The smallest rubric total whose percentage reaches the auto-publish threshold."""
    for total in range(RUBRIC_MAX + 1):
        if _rubric_percent(total) >= min_score:
            return total
    return RUBRIC_MAX


def rubric_decision(rubric: Mapping[str, Any], min_score: int) -> tuple[str, int]:
    total = sum(rubric[key] for key in RUBRIC_CRITERIA)
    score = _rubric_percent(total)
    needed = needed_total(min_score)
    if _hard_reject_reason(rubric):
        return "reject", score
    if total >= needed:
        return "publish", score
    # One point short: rejected on timeout like any non-publish verdict, but shown to moderators as a close call.
    return ("review" if total == needed - 1 else "reject"), score


def _hard_reject_reason(rubric: Mapping[str, Any]) -> str | None:
    """Why a post is rejected whatever its total: a flag, high authenticity risk, or a weak criterion."""
    if rubric["flags"]:
        return "flags"
    if rubric["authenticity_risk"] == "high":
        return "authenticity"
    if min(rubric[key] for key in RUBRIC_CRITERIA) < RUBRIC_MIN_CRITERION_TO_PUBLISH:
        return "criterion"
    return None


HARD_REJECT_LABELS = {
    "en": {"flags": "flagged", "authenticity": "high authenticity risk", "criterion": "a score below 2"},
    "ru": {"flags": "есть флаг", "authenticity": "высокий риск неаутентичности", "criterion": "оценка ниже 2"},
}


def _rubric_percent(total: int) -> int:
    return round(total * 100 / RUBRIC_MAX)


def _min_score(ai_settings: Mapping[str, Any] | None) -> int:
    try:
        value = int((ai_settings or {}).get("auto_publish_min_score", DEFAULT_MIN_SCORE))
    except (TypeError, ValueError):
        value = DEFAULT_MIN_SCORE
    return max(50, min(100, value))


def _rubric_borderline(rubric: Mapping[str, Any], min_score: int) -> bool:
    if rubric["flags"]:
        return False
    total = sum(rubric[key] for key in RUBRIC_CRITERIA)
    needed = needed_total(min_score)
    if needed - 1 <= total <= needed:
        return True
    # A publishable post held back only by "high" authenticity risk deserves a second opinion too.
    return rubric["authenticity_risk"] == "high" and total >= needed


def _parse_rubric(value: str) -> dict[str, Any] | None:
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(value[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    rubric: dict[str, Any] = {}
    for key in RUBRIC_CRITERIA:
        try:
            rubric[key] = max(0, min(RUBRIC_CRITERION_MAX, int(data[key])))
        except (KeyError, TypeError, ValueError):
            return None

    flags = data.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    normalized = {str(flag).strip().lower().replace("-", "_").replace(" ", "_") for flag in flags}
    rubric["flags"] = [flag for flag in HARD_FLAGS if flag in normalized]

    risk = str(data.get("authenticity_risk") or "").strip().lower()
    rubric["authenticity_risk"] = risk if risk in AUTHENTICITY_LEVELS else "medium"
    for key in ("signals", "risks", "author_note"):
        rubric[key] = _single_line(data.get(key), 300)
    return rubric


def _combine_rubrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if len(samples) == 1:
        return samples[0]
    combined: dict[str, Any] = {key: statistics.median_low(s[key] for s in samples) for key in RUBRIC_CRITERIA}
    combined["flags"] = [flag for flag in HARD_FLAGS if sum(flag in s["flags"] for s in samples) * 2 > len(samples)]
    combined["authenticity_risk"] = AUTHENTICITY_LEVELS[
        statistics.median_low(AUTHENTICITY_LEVELS.index(s["authenticity_risk"]) for s in samples)
    ]
    # Explanations come from the run that agrees best with the combined scores.
    total = sum(combined[key] for key in RUBRIC_CRITERIA)
    closest = min(samples, key=lambda s: abs(sum(s[key] for key in RUBRIC_CRITERIA) - total))
    for key in ("signals", "risks", "author_note"):
        combined[key] = closest[key]
    return combined


def _render_rubric(rubric: Mapping[str, Any], recommendation: str, score: int, runs: int, language: str) -> str:
    language = "en" if language == "en" else "ru"
    total = sum(rubric[key] for key in RUBRIC_CRITERIA)
    criteria = " · ".join(
        f"{CRITERION_LABELS[language][key]} {rubric[key]}/{RUBRIC_CRITERION_MAX}" for key in RUBRIC_CRITERIA
    )
    if runs > 1:
        criteria += f" · {'checked' if language == 'en' else 'проверено'} {runs}×"
    verdict = VERDICT_LABELS[language][recommendation]
    hard_reason = _hard_reject_reason(rubric)
    if hard_reason:
        verdict += f" ({HARD_REJECT_LABELS[language][hard_reason]})"
    authenticity = AUTHENTICITY_LABELS[language][rubric["authenticity_risk"]]
    if language == "en":
        lines = [
            f"🧭 Verdict: {verdict} | score {total}/{RUBRIC_MAX} ({score}%)",
            f"📐 {criteria}",
            f"🧬 Authenticity risk: {authenticity}",
        ]
        if rubric["flags"]:
            lines.append("🚩 Flags: " + ", ".join(FLAG_LABELS[language][flag] for flag in rubric["flags"]))
        lines += [f"🧪 Signals: {rubric['signals'] or '-'}", f"⚠️ Risks: {rubric['risks'] or 'none'}"]
        if recommendation != "publish" and rubric["author_note"]:
            lines.append(f"💬 For author: {rubric['author_note']}")
        return "\n".join(lines)

    lines = [
        f"🧭 Вердикт: {verdict} | оценка {total}/{RUBRIC_MAX} ({score}%)",
        f"📐 {criteria}",
        f"🧬 Риск неаутентичности: {authenticity}",
    ]
    if rubric["flags"]:
        lines.append("🚩 Флаги: " + ", ".join(FLAG_LABELS[language][flag] for flag in rubric["flags"]))
    lines += [f"🧪 Сигналы: {rubric['signals'] or '-'}", f"⚠️ Риски: {rubric['risks'] or 'нет'}"]
    if recommendation != "publish" and rubric["author_note"]:
        lines.append(f"💬 Автору: {rubric['author_note']}")
    return "\n".join(lines)


def _single_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
