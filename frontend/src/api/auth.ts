// 这个文件定义了认证相关的 API 接口，包括登录、注册和发送验证码。
import request from '../utils/request'
import type { LoginData, RegisterData, LoginResponse, SendCodeData } from '../types/api'

// 登录接口
export function login(data: LoginData) {
  return request<LoginResponse>({
    url: '/auth/login',
    method: 'post',
    data
  })
}

// 注册接口
export function register(data: RegisterData) {
  return request<any>({
    url: '/auth/register',
    method: 'post',
    data
  })
}

// 发送验证码接口
export function sendCode(data: SendCodeData) {
  return request<any>({
    url: '/auth/send-code',
    method: 'post',
    data
  })
}

// 获取当前用户信息
export function getUserInfo() {
  return request<any>({
    url: '/auth/me',
    method: 'get'
  })
}
