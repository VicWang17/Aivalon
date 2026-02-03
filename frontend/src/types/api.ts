// 这个文件是前端API接口的通用类型定义，对应后端的ResponseModel，用于Axios请求响应的数据类型推断。
// 统一 API 响应结构
export interface ApiResponse<T = any> {
  code: number;
  message: string;
  data: T;
}

// 业务错误码定义
export const ErrorCode = {
  SUCCESS: 0,
  UNAUTHORIZED: 401,
  FORBIDDEN: 403,
  VALIDATION_ERROR: 422,
  GAME_NOT_FOUND: 10001,
  INVALID_ACTION: 10002,
} as const;

export type ErrorCodeType = typeof ErrorCode[keyof typeof ErrorCode];
