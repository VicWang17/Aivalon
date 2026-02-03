# 这个文件是通用的API响应数据模型定义，规范了HTTP接口返回的JSON结构（code, message, data）。
from typing import Generic, TypeVar, Optional
from pydantic import BaseModel

T = TypeVar("T")

class ResponseModel(BaseModel, Generic[T]):
    """
    统一 API 响应模型
    
    Attributes:
        code (int): 业务状态码，0 表示成功，非 0 表示错误
        message (str): 提示信息
        data (T | None): 响应数据
    """
    code: int = 0
    message: str = "success"
    data: Optional[T] = None

    class Config:
        # 允许通过字段名访问
        from_attributes = True
