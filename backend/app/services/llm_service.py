"""
这个文件是 LLM 服务的封装，负责调用 DeepSeek API 生成内容。
"""
import json
from typing import Optional, Dict, Any, List
from openai import AsyncOpenAI
from app.core.config import settings

class LLMService:
    _client: Optional[AsyncOpenAI] = None

    @classmethod
    def get_client(cls) -> AsyncOpenAI:
        if cls._client is None:
            if not settings.DEEPSEEK_API_KEY:
                raise ValueError("DEEPSEEK_API_KEY is not set in configuration")
            
            cls._client = AsyncOpenAI(
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.DEEPSEEK_BASE_URL
            )
        return cls._client

    @classmethod
    async def generate_response(
        cls, 
        system_prompt: str, 
        user_prompt: str, 
        json_mode: bool = True,
        temperature: float = 0.7,
        timeout: int = 30  # 新增超时参数
    ) -> Dict[str, Any]:
        """
        调用 LLM 生成响应
        :param system_prompt: 系统提示词（包含角色设定、输出格式要求）
        :param user_prompt: 用户提示词（包含当前局势、历史记录）
        :param json_mode: 是否强制 JSON 格式输出
        :param temperature: 采样温度，越高越随机（0.0 - 2.0）
        :param timeout: 超时时间（秒）
        :return: 解析后的字典或原始文本
        """
        client = cls.get_client()
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        try:
            response = await client.chat.completions.create(
                model=settings.DEEPSEEK_MODEL,
                messages=messages,
                response_format={"type": "json_object"} if json_mode else None,
                temperature=temperature,
                max_tokens=1024,
                timeout=timeout # 设置超时
            )
            
            content = response.choices[0].message.content
            
            if json_mode:
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    print(f"[LLM] JSON Parse Error. Content: {content}")
                    # 尝试修复或返回错误结构
                    return {"error": "Invalid JSON response", "raw_content": content}
            else:
                return {"content": content}
                
        except Exception as e:
            print(f"[LLM] API Call Error or Timeout: {e}")
            return {"error": str(e)}
