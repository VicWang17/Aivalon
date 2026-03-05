<!-- 这个文件是注册页面，实现了用户注册功能，包含发送验证码、表单验证和样式。 -->
<template>
  <div class="register-container flex-center">
    <div class="glass-panel register-card">
      <h2 class="title">加入阿瓦隆</h2>
      <form @submit.prevent="handleRegister" class="register-form">
        <div class="form-group">
          <label for="username" class="text-secondary">用户名</label>
          <input 
            id="username"
            v-model="registerForm.username" 
            type="text" 
            placeholder="请输入骑士名讳"
            required
            minlength="3"
          />
        </div>

        <div class="form-group">
          <label for="email" class="text-secondary">电子邮箱</label>
          <div class="input-with-button">
            <input 
              id="email"
              v-model="registerForm.email" 
              type="email" 
              placeholder="请输入邮箱地址"
              required
            />
            <button 
              type="button" 
              class="btn-ghost code-btn"
              :disabled="codeCooldown > 0 || !isValidEmail"
              @click="handleSendCode"
            >
              {{ codeCooldown > 0 ? `${codeCooldown}s` : '发送验证码' }}
            </button>
          </div>
        </div>

        <div class="form-group">
          <label for="code" class="text-secondary">验证码</label>
          <input 
            id="code"
            v-model="registerForm.verification_code" 
            type="text" 
            placeholder="请查看邮箱验证码"
            required
            maxlength="6"
          />
        </div>
        
        <div class="form-group">
          <label for="password" class="text-secondary">密码</label>
          <input 
            id="password"
            v-model="registerForm.password" 
            type="password" 
            placeholder="请设置您的密语"
            required
            minlength="6"
          />
        </div>

        <div v-if="errorMessage" class="error-message text-danger">
          {{ errorMessage }}
        </div>
        <div v-if="successMessage" class="success-message text-success">
          {{ successMessage }}
        </div>

        <button 
          type="submit" 
          class="btn-primary w-full mt-4"
          :disabled="loading"
        >
          <span v-if="loading">正在铸造身份...</span>
          <span v-else>注册</span>
        </button>

        <div class="mt-4 text-center">
          <span class="text-secondary">已有账号？ </span>
          <router-link to="/login" class="text-accent">前往登录</router-link>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { register, sendCode } from '../api/auth'

const router = useRouter()

const registerForm = reactive({
  username: '',
  email: '',
  password: '',
  verification_code: ''
})

const loading = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const codeCooldown = ref(0)
let timer: any = null

const isValidEmail = computed(() => {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(registerForm.email)
})

const handleSendCode = async () => {
  if (!isValidEmail.value) return
  
  try {
    await sendCode({ email: registerForm.email })
    startCooldown()
    successMessage.value = '验证码已发送！'
    errorMessage.value = ''
  } catch (error: any) {
    // 优先展示后端返回的错误信息（包括限流提示）
    errorMessage.value = error.response?.data?.detail || error.message || '发送验证码失败'
    successMessage.value = ''
    
    // 如果是 429 错误，自动进入冷却倒计时（体验优化）
    if (error.response?.status === 429) {
      startCooldown()
    }
  }
}

const startCooldown = () => {
  codeCooldown.value = 60
  timer = setInterval(() => {
    codeCooldown.value--
    if (codeCooldown.value <= 0) {
      clearInterval(timer)
    }
  }, 1000)
}

const handleRegister = async () => {
  if (!registerForm.username || !registerForm.email || !registerForm.password || !registerForm.verification_code) {
    errorMessage.value = '请填写所有字段'
    return
  }

  loading.value = true
  errorMessage.value = ''
  successMessage.value = ''

  try {
    await register(registerForm)
    successMessage.value = '注册成功！正在跳转登录...'
    setTimeout(() => {
      router.push('/login')
    }, 1500)
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
      errorMessage.value = '注册失败，请重试'
    }
  } finally {
    loading.value = false
  }
}

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.register-container {
  min-height: 100vh;
  padding: 2rem;
  box-sizing: border-box;
}

.register-card {
  width: 100%;
  max-width: 450px;
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

.input-with-button {
  display: flex;
  gap: 0.5rem;
}

.code-btn {
  white-space: nowrap;
  min-width: 100px;
}

.error-message {
  font-size: 0.9rem;
  margin-top: 0.5rem;
  text-align: center;
}

.success-message {
  font-size: 0.9rem;
  margin-top: 0.5rem;
  text-align: center;
  color: var(--color-success);
}

.text-center {
  text-align: center;
}
</style>
