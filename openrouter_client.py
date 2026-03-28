import json
import urllib.request
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def generate_with_openrouter(prompt, model="z-ai/glm-4.5-air:free", system_prompt=None):
    """Generates a response from OpenRouter API asynchronously."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return "[AI Error: OPENROUTER_API_KEY missing in .env]"
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
    }
        
    data = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {api_key}",
        'HTTP-Referer': 'https://github.com/zarvisaz007/TridenB_autoforwarderBETA',
        'X-Title': 'TridenB Autoforwarder'
    }
    req = urllib.request.Request(url, data=data, headers=headers)

    def _make_request():
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result['choices'][0]['message']['content'].strip()
        except Exception as e:
            return f"[AI Error: {e}]"

    return await asyncio.to_thread(_make_request)
