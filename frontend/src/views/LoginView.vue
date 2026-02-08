<!-- 这个文件是登录页面，实现了用户登录功能，包含表单验证和样式。 -->
<template>
  <div class="login-container flex-center">
    <div class="glass-panel login-card">
      <h2 class="title">阿瓦隆 · 登录</h2>
      <form @submit.prevent="handleLogin" class="login-form">
        <div class="form-group">
          <label for="username" class="text-secondary">用户名</label>
          <input 
            id="username"
            v-model="loginForm.username" 
            type="text" 
            placeholder="请输入用户名"
            required
          />
        </div>
        
        <div class="form-group">
          <label for="password" class="text-secondary">密码</label>
          <input 
            id="password"
            v-model="loginForm.password" 
            type="password" 
            placeholder="请输入密码"
            required
          />
        </div>

        <div v-if="errorMessage" class="error-message text-danger">
          {{ errorMessage }}
        </div>

        <button 
          type="submit" 
          class="btn-primary w-full mt-4"
          :disabled="loading"
        >
          <span v-if="loading">正在进入...</span>
          <span v-else>进入圆桌会议</span>
        </button>

        <div class="mt-4 text-center">
          <span class="text-secondary">初次来到阿瓦隆？ </span>
          <router-link to="/register" class="text-accent">加入远征</router-link>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '../store/user'

const router = useRouter()
const userStore = useUserStore()

const loginForm = reactive({
  username: '',
  password: ''
})

const loading = ref(false)
const errorMessage = ref('')

const handleLogin = async () => {
  if (!loginForm.username || !loginForm.password) {
    errorMessage.value = '请填写所有字段'
    return
  }

  loading.value = true
  errorMessage.value = ''

  try {
    await userStore.login(loginForm)
    router.push('/')
  } catch (error: any) {
    // 处理后端返回的错误信息
    if (error.response?.data?.detail) {
      const detail = error.response.data.detail
      if (Array.isArray(detail)) {
        // 如果是 Validation Error 数组，取第一个错误显示
        errorMessage.value = detail.map((e: any) => e.msg).join('; ')
      } else {
        // 如果是普通错误信息
        errorMessage.value = detail
      }
    } else {
      errorMessage.value = '登录失败，请重试'
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  min-height: 100vh;
  padding: 2rem;
  box-sizing: border-box;
}

.login-card {
  width: 100%;
  max-width: 400px;
  padding: 2.5rem;
}

.title {
  text-align: center;
  margin-bottom: 2rem;
  font-size: 2rem;
  color: var(--color-text-accent);
}

.form-group {
  margin-bottom: 1.5rem;
}

.form-group label {
  display: block;
  margin-bottom: 0.5rem;
  font-size: 0.9rem;
  text-transform: uppercase;
  letter-spacing: 1px;
}

.error-message {
  font-size: 0.9rem;
  margin-top: 0.5rem;
  text-align: center;
}

.text-center {
  text-align: center;
}
</style>
