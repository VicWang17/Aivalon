// 这个文件定义了 User Store，使用 Pinia 管理用户的登录状态、Token 和个人信息。
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getToken, setToken, removeToken } from '../utils/auth'
// import { login as loginApi, getUserInfo as getUserInfoApi } from '../api/user' // 假设有 api 定义

export const useUserStore = defineStore('user', () => {
  // State
  const token = ref(getToken() || '')
  const username = ref('')
  const userId = ref<number | null>(null)

  // Actions
  function login(newToken: string) {
    token.value = newToken
    setToken(newToken)
  }

  function logout() {
    token.value = ''
    username.value = ''
    userId.value = null
    removeToken()
  }

  // 模拟从后端获取用户信息并存入 Store
  function setUserInfo(info: { id: number; username: string }) {
    userId.value = info.id
    username.value = info.username
  }

  return {
    token,
    username,
    userId,
    login,
    logout,
    setUserInfo
  }
})
