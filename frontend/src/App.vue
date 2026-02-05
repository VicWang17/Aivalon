<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useUserStore } from './store/user'
import request from './utils/request'

const userStore = useUserStore()
const usernameInput = ref('')
const passwordInput = ref('')
const message = ref('')

async function handleLogin() {
  try {
    const res: any = await request.post('/auth/login', {
      username: usernameInput.value,
      password: passwordInput.value
    })
    
    // 登录成功，保存 Token 到 Store
    userStore.login(res.data.access_token)
    message.value = '登录成功！Token 已保存。'
    
    // 登录后立即获取用户信息
    await fetchUserInfo()
    
  } catch (error: any) {
    message.value = '登录失败: ' + (error.message || '未知错误')
  }
}

async function fetchUserInfo() {
  try {
    const res: any = await request.get('/auth/me')
    userStore.setUserInfo(res.data)
    message.value += `\n获取用户信息成功: ${res.data.username}`
  } catch (error: any) {
    message.value += `\n获取用户信息失败`
  }
}

function handleLogout() {
  userStore.logout()
  message.value = '已退出登录'
}
</script>

<template>
  <div class="container">
    <h1>Aivalon 前端鉴权测试</h1>
    
    <div class="card">
      <h2>状态面板</h2>
      <p><strong>Token:</strong> {{ userStore.token ? '已存在 (Bearer ...)' : '未登录' }}</p>
      <p><strong>当前用户:</strong> {{ userStore.username || '未知' }}</p>
      <p class="message">{{ message }}</p>
    </div>

    <div v-if="!userStore.token" class="card">
      <h2>登录</h2>
      <input v-model="usernameInput" placeholder="用户名" />
      <input v-model="passwordInput" type="password" placeholder="密码" />
      <button @click="handleLogin">登录</button>
    </div>

    <div v-else class="card">
      <button @click="fetchUserInfo">测试 /me 接口 (自动带Token)</button>
      <button @click="handleLogout" style="background-color: #ff4444;">退出登录</button>
    </div>
  </div>
</template>

<style scoped>
.container {
  max-width: 600px;
  margin: 0 auto;
  padding: 2rem;
  font-family: Arial, sans-serif;
}
.card {
  border: 1px solid #ccc;
  border-radius: 8px;
  padding: 1.5rem;
  margin-bottom: 1rem;
  background: #f9f9f9;
}
input {
  display: block;
  width: 100%;
  padding: 0.5rem;
  margin-bottom: 0.5rem;
  box-sizing: border-box;
}
button {
  padding: 0.5rem 1rem;
  margin-right: 0.5rem;
  cursor: pointer;
  background-color: #42b883;
  color: white;
  border: none;
  border-radius: 4px;
}
button:hover {
  opacity: 0.9;
}
.message {
  white-space: pre-wrap;
  color: #666;
  font-size: 0.9em;
}
h1, h2 {
  color: #333;
}
</style>
