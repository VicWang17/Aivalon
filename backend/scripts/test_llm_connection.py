import sys
import os
import json

# Add backend directory to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Ensure we load config which loads .env
from app.core.config import settings
from app.services.llm_service import LLMService

print("--- Testing DeepSeek API Connection ---")
print(f"API Key Loaded: {'Yes' if settings.DEEPSEEK_API_KEY else 'No'}")
if settings.DEEPSEEK_API_KEY:
    print(f"API Key Prefix: {settings.DEEPSEEK_API_KEY[:6]}...")

try:
    print("\nSending test request to DeepSeek API...")
    # Simple test prompt
    system_prompt = "你是一个乐于助人的助手。"
    user_prompt = "请用 JSON 格式说 '你好世界': {'message': '你好世界'}"
    
    response = LLMService.generate_response(system_prompt, user_prompt, json_mode=True)
    
    print("\n[Response]:")
    print(json.dumps(response, indent=2, ensure_ascii=False))
    
    if "error" in response:
        print("\n[FAILURE]: API returned an error.")
    else:
        print("\n[SUCCESS]: API call successful.")
        
except Exception as e:
    print(f"\n[EXCEPTION]: {e}")
