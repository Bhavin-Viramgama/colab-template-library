"""
AI Chat Runner for Google Colab.

Loads a compact Hugging Face instruct model (default: Qwen/Qwen2.5-1.5B-Instruct)
once into Colab memory/VRAM and supports both token-by-token streaming (via HTTP SSE)
and single-turn task execution (via Drive status.json fallback).
"""

import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Generator, List, Optional

DEFAULT_MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"


class AiChatManager:
    """Singleton-style lazy model loader and generator for Colab."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model_id: Optional[str] = None
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = "cpu"
        self._loading: bool = False
        self._error: Optional[str] = None

    def get_status(self) -> Dict[str, Any]:
        return {
            "loaded": self._model is not None,
            "loading": self._loading,
            "modelId": self._model_id or DEFAULT_MODEL_ID,
            "device": self._device,
            "error": self._error,
        }

    def _ensure_dependencies(self) -> None:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "transformers", "accelerate", "torch"],
                check=True,
            )

    def ensure_loaded(self, model_id: Optional[str] = None) -> None:
        target_model = (model_id or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
        with self._lock:
            if self._model is not None and self._model_id == target_model:
                return
            self._loading = True
            self._error = None
            try:
                self._ensure_dependencies()
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer

                use_cuda = torch.cuda.is_available()
                self._device = "cuda" if use_cuda else "cpu"
                dtype = torch.float16 if use_cuda else torch.float32

                tokenizer = AutoTokenizer.from_pretrained(target_model)
                model = AutoModelForCausalLM.from_pretrained(
                    target_model,
                    torch_dtype=dtype,
                    device_map="auto" if use_cuda else None,
                    low_cpu_mem_usage=True,
                )
                if not use_cuda:
                    model.to("cpu")
                model.eval()

                self._tokenizer = tokenizer
                self._model = model
                self._model_id = target_model
            except Exception as exc:
                self._error = str(exc)
                raise
            finally:
                self._loading = False

    def _build_prompt_inputs(self, messages: List[Dict[str, str]]) -> Any:
        assert self._tokenizer is not None and self._model is not None
        cleaned = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in messages
            if (m.get("content") or "").strip()
        ]
        if not cleaned:
            cleaned = [{"role": "user", "content": "Hello"}]

        if hasattr(self._tokenizer, "apply_chat_template"):
            prompt_text = self._tokenizer.apply_chat_template(
                cleaned, tokenize=False, add_generation_prompt=True
            )
        else:
            prompt_text = "\n".join(f"{m['role']}: {m['content']}" for m in cleaned) + "\nassistant:"

        encoded = self._tokenizer(prompt_text, return_tensors="pt")
        device = next(self._model.parameters()).device
        return encoded.to(device)

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model_id: Optional[str] = None,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
    ) -> Generator[str, None, None]:
        """Yield generated text tokens incrementally using TextIteratorStreamer."""
        self.ensure_loaded(model_id)
        from transformers import TextIteratorStreamer

        model_inputs = self._build_prompt_inputs(messages)
        streamer = TextIteratorStreamer(
            self._tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
        )

        gen_kwargs = {
            **model_inputs,
            "streamer": streamer,
            "max_new_tokens": max(32, min(int(max_new_tokens), 1024)),
            "do_sample": temperature > 0.05,
            "temperature": max(0.05, float(temperature)) if temperature > 0.05 else 1.0,
            "top_p": 0.9,
            "pad_token_id": self._tokenizer.eos_token_id,
        }

        thread = threading.Thread(target=self._model.generate, kwargs=gen_kwargs, daemon=True)
        thread.start()
        for piece in streamer:
            if piece:
                yield piece
        thread.join()

    def generate_reply(
        self,
        messages: List[Dict[str, str]],
        model_id: Optional[str] = None,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        chunks = list(
            self.stream_chat(
                messages=messages,
                model_id=model_id,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
            )
        )
        return "".join(chunks).strip()


_MANAGER: Optional[AiChatManager] = None


def get_ai_chat_manager() -> AiChatManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = AiChatManager()
    return _MANAGER


def run_chat_task(
    prompt: str,
    task_id: str,
    model_id: Optional[str] = None,
    status_dir: str = "/content/drive/MyDrive/.colab-tasks",
) -> Dict[str, Any]:
    """Execute an ai-chat task via status.json (fallback when tunnel isn't active)."""
    import json

    status_file = os.path.join(status_dir, task_id, "status.json")
    os.makedirs(os.path.dirname(status_file), exist_ok=True)

    def _write(payload: Dict[str, Any]) -> None:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    _write({
        "taskId": task_id,
        "status": "running",
        "progress": 15,
        "progressMessage": "Loading AI model in Colab...",
        "lastUpdated": time.time(),
    })

    mgr = get_ai_chat_manager()
    accumulated = []
    last_flush = time.time()
    for token in mgr.stream_chat([{"role": "user", "content": prompt}], model_id=model_id):
        accumulated.append(token)
        if time.time() - last_flush >= 0.8:
            _write({
                "taskId": task_id,
                "status": "running",
                "progress": 60,
                "progressMessage": "".join(accumulated),
                "lastUpdated": time.time(),
            })
            last_flush = time.time()

    full_text = "".join(accumulated).strip()
    _write({
        "taskId": task_id,
        "status": "completed",
        "progress": 100,
        "progressMessage": full_text,
        "outputPath": full_text,
        "lastUpdated": time.time(),
    })
    return {"success": True, "response": full_text}
