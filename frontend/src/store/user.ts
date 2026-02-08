// 这个文件定义了 User Store，使用 Pinia 管理用户的登录状态、Token 和个人信息。
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getToken, setToken, removeToken } from '../utils/auth'
import { login as loginApi, getUserInfo as getUserInfoApi } from '../api/auth'
import type { LoginData, UserInfo } from '../types/api'

export const useUserStore = defineStore('user', () => {
  // State
  const token = ref(getToken() || '')
  const username = ref('')
  const userId = ref<number | null>(null)
  const email = ref('')

  // Getters
  const userInfo = computed<UserInfo | null>(() => {
    if (!userId.value) return null
    return {
      id: userId.value,
      username: username.value,
      email: email.value,
      is_active: true, // 简化处理
      created_at: ''
    }
  })

  // Actions
  async function login(loginData: LoginData) {
    try {
      const res = await loginApi(loginData)
      const accessToken = res.data.access_token
      token.value = accessToken
      setToken(accessToken)
      // 登录成功后获取用户信息
      await fetchUserInfo()
      return true
    } catch (error) {
      console.error('Login failed:', error)
      throw error
    }
  }

  async function fetchUserInfo() {
    try {
      const res = await getUserInfoApi()
      const userInfo = res.data
      userId.value = userInfo.id
      username.value = userInfo.username
      email.value = userInfo.email
      return userInfo
    } catch (error) {
      console.error('Get user info failed:', error)
      throw error
    }
  }

  function logout() {
    token.value = ''
    username.value = ''
    userId.value = null
    email.value = ''
    removeToken()
  }

  // 模拟从后端获取用户信息并存入 Store
  function setUserInfo(info: { id: number; username: string; email: string }) {
    userId.value = info.id
    username.value = info.username
    email.value = info.email
  }

  return {
    token,
    username,
    userId,
    email,
    userInfo,
    login,
    fetchUserInfo,
    logout,
    setUserInfo
  }
})
