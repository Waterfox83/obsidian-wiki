import json
import os
import urllib.error
import urllib.request

LM_BASE_URL = os.getenv("LM_BASE_URL", "http://127.0.0.1:1234")
LM_MODEL = os.getenv("LM_MODEL", "google/gemma-4-26b-a4b")
LM_TIMEOUT_SECONDS = int(os.getenv("LM_TIMEOUT_SECONDS", "60"))


def call_lm_studio(prompt: str) -> str:
    url = f"{LM_BASE_URL.rstrip('/')}/v1/chat/completions"

    payload = {
        "model": LM_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "stream": False,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=LM_TIMEOUT_SECONDS) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    if "choices" not in body or not body["choices"]:
        raise RuntimeError(f"Unexpected response schema: {body}")

    return body["choices"][0]["message"]["content"]


def main() -> None:
    prompt = "hi, how are you?"

    print(f"Testing LM Studio at: {LM_BASE_URL}")
    print(f"Model: {LM_MODEL}")

    try:
        output = call_lm_studio(prompt)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        print(f"HTTP error: {e.code} {e.reason}")
        print("Response body:")
        print(error_body)
        raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e}")
        raise SystemExit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        raise SystemExit(1)

    print("--- Model Output ---")
    print(output)
    print("--------------------")
    print("LM Studio test completed successfully.")


if __name__ == "__main__":
    main()
