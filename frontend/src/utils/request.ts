// 这个文件封装了 Axios 实例，配置了全局拦截器，用于自动携带 Token 和处理错误。
import axios from 'axios'
import { getToken } from './auth'

// 创建 axios 实例
const service = axios.create({
  baseURL: '/api/v1', // 配合 Vite 代理或 Nginx
  timeout: 5000 // 请求超时时间
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
    const res = response.data
    // 这里可以根据后端的 code 做统一判断
    // 假设 code === 0 为成功
    if (res.code !== 0) {
      console.error('API Error:', res.message)
      // 可以结合 UI 组件库弹出错误提示
      return Promise.reject(new Error(res.message || 'Error'))
    } else {
      return res
    }
  },
  error => {
    console.error('Request Error:', error)
    // 可以在这里处理 401 (未授权) 跳转登录页等逻辑
    if (error.response && error.response.status === 401) {
       // TODO: 清除 Token 并跳转登录页
    }
    return Promise.reject(error)
  }
)

export default service
