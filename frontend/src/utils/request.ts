// 这个文件封装了 Axios 实例，配置了全局拦截器，用于自动携带 Token 和处理错误。
import axios from 'axios'
import { getToken } from './auth'

// 创建 axios 实例
const service = axios.create({
  baseURL: '/api/v1', // 配合 Vite 代理或 Nginx
  timeout: 15000 // 请求超时时间 (15s)
})

// request 拦截器
service.interceptors.request.use(
  config => {
    // 在发送请求之前做些什么
    const token = getToken()
    if (token) {
      // 让每个请求携带 token
      // Bearer 是 JWT 的标准规范前缀
      config.headers['Authorization'] = `Bearer ${token}`
    }
    return config
  },
  error => {
    // 对请求错误做些什么
    console.log(error)
    return Promise.reject(error)
  }
)

// response 拦截器
service.interceptors.response.use(
  response => {
    // 兼容 FastAPI 直接返回数据的情况（非统一 Response 结构）
    // 如果返回的不是 { code, message, data } 结构，直接返回 data
    const res = response.data
    
    // 检查是否存在业务 code
    if (res && typeof res.code === 'number') {
      if (res.code !== 0) {
        console.error('API Error:', res.message)
        return Promise.reject(new Error(res.message || 'Error'))
      }
      return res
    }
    
    // 如果没有 code 字段，说明是直接返回的数据（如 Pydantic Model），直接返回 response.data
    // 这样业务层就不需要再解包 AxiosResponse 了
    return response.data
  },
  error => {
    console.error('Request Error:', error)
    // 可以在这里处理 401 (未授权) 跳转登录页等逻辑
    if (error.response && error.response.status === 401) {
      // 避免循环引用，这里直接操作 localStorage 和 window.location
      localStorage.removeItem('aivalon_token')
      // 如果当前不在登录页，则跳转
      if (!window.location.pathname.includes('/login')) {
        window.location.href = '/login'
      }
    }
    // 处理 429 Too Many Requests
    if (error.response && error.response.status === 429) {
      const retryAfter = error.response.headers['retry-after'] || '60'
      const msg = error.response.data?.detail || `操作过于频繁，请在 ${retryAfter} 秒后重试`
      // 这里可以接入全局消息提示组件，暂时先 reject 出去让业务层处理
      // 也可以修改 error message 让上层显示更友好
      error.message = msg
      // 也可以直接在这里修改 error.response.data.detail 确保上层拿到的是友好信息
      if (error.response.data) {
        error.response.data.detail = msg
      }
    }
    return Promise.reject(error)
  }
)

export default service
