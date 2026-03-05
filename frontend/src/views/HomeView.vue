<!-- 这个文件是应用首页，包含游戏入口和用户信息展示。 -->
<template>
  <div class="home-container">
    <!-- 顶部导航栏 -->
    <nav class="top-nav glass-panel">
      <div class="nav-left">
        <span class="nav-brand">Aivalon</span>
      </div>
      <div class="nav-right">
        <div class="user-profile" v-if="userStore.username">
          <div class="avatar-frame">
            <span class="avatar-text">{{ userStore.username.charAt(0).toUpperCase() }}</span>
          </div>
          <span class="username text-secondary">{{ userStore.username }}</span>
        </div>
        <button @click="handleLogout" class="btn-ghost logout-btn">
          退出
        </button>
      </div>
    </nav>

    <!-- 核心内容区 -->
    <main class="hero-section flex-center">
      <div class="hero-content text-center">
        <h1 class="main-title gradient-text">AI阿瓦隆</h1>
        <p class="subtitle text-secondary">正义与邪恶的终极较量</p>
        
        <div class="action-area mt-8">
          <button class="btn-primary btn-large glow-effect" @click="handleStartGame">
            <span class="icon">⚔️</span>
            <span>开启圆桌会议</span>
          </button>
          
          <router-link to="/history" class="btn-ghost btn-large mt-4">
            <span class="icon">📜</span>
            <span>查看对局历史</span>
          </router-link>
          
          <router-link to="/leaderboard" class="btn-ghost btn-large mt-4">
            <span class="icon">🏆</span>
            <span>胜场排行榜</span>
          </router-link>
        </div>
      </div>
    </main>
    
    <!-- 装饰性背景元素 -->
    <div class="bg-overlay"></div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '../store/user'
import { createGame } from '../api/game'

const router = useRouter()
const userStore = useUserStore()

onMounted(async () => {
  if (!userStore.username) {
    try {
      await userStore.fetchUserInfo()
    } catch (error) {
      console.error('Failed to fetch user info', error)
      // 如果获取失败（token过期等），跳转登录
      handleLogout()
    }
  }
})

const handleLogout = () => {
  userStore.logout()
  router.push('/login')
}

const handleStartGame = async () => {
  if (!userStore.userInfo) return
  
  try {
    // 构造8人局ID列表：自己 + 7个机器人ID
    const myId = userStore.userInfo.id
    // 使用简单的偏移量生成机器人ID，确保不重复
    const botIds = Array.from({ length: 7 }, (_, i) => myId + 1000 + i)
    const playerIds = [myId, ...botIds]
    
    const res = await createGame({ player_ids: playerIds })
    console.log('Create Game Response:', res)

    // 调试：打印响应结构
    console.log('Create Game Raw Response:', res)
    
    // 兼容多种响应结构：
    // 1. res 是 AxiosResponse，数据在 res.data
    // 2. res 直接是数据对象（拦截器处理后）
    // 3. 数据可能在 res.data.data (标准API结构) 或 res.data (直接Pydantic模型)
    
    let gameId: string | undefined
    
    // 安全地获取响应体
    const responseBody = (res && (res as any).data) ? (res as any).data : res
    console.log('Processed Response Body:', responseBody)
    
    // 尝试从不同层级提取 game_id
    if (responseBody) {
      // Case 1: 直接在 responseBody 中 (例如拦截器返回了 res.data，且 res.data 就是 payload)
      if (responseBody.game_id) {
        gameId = responseBody.game_id
      }
      // Case 2: 在 responseBody.data 中 (例如拦截器返回了 res，且 res 是 ResponseModel)
      else if (responseBody.data && responseBody.data.game_id) {
        gameId = responseBody.data.game_id
      }
      // Case 3: 可能是嵌套的 (例如 AxiosResponse -> ResponseModel -> Payload)
      // 如果 responseBody 是 ResponseModel (code, data)，而 data 又是对象
      else if (responseBody.code === 0 && responseBody.data && responseBody.data.game_id) {
         gameId = responseBody.data.game_id
      }
    }
    
    console.log('Extracted Game ID:', gameId)
    
    if (gameId) {
      console.log('Redirecting to game:', gameId)
      router.push(`/game/${gameId}`).catch(err => {
        console.error('Router push failed:', err)
        alert(`跳转失败: ${err.message}`)
      })
    } else {
      console.error('Invalid response structure:', res)
      alert(`创建对局失败：无法获取 game_id。响应数据: ${JSON.stringify(res)}`)
      throw new Error('No game_id returned from server')
    }
  } catch (error: any) {
    console.error('Create game failed', error)
    const errorMsg = error.response?.data?.detail || error.message || '未知错误'
    alert(`创建对局失败: ${errorMsg}`)
  }
}
</script>

<style scoped>
.home-container {
  min-height: 100vh;
  position: relative;
  overflow: hidden;
  background: var(--color-bg-main);
}

/* 顶部导航 */
.top-nav {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: 64px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 2rem;
  z-index: 100;
  border-bottom: 1px solid rgba(71, 85, 105, 0.3);
}

.nav-brand {
  font-family: 'Cinzel', serif;
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-accent);
  letter-spacing: 2px;
}

.nav-right {
  display: flex;
  align-items: center;
  gap: 1.5rem;
}

.user-profile {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.avatar-frame {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: 2px solid var(--text-accent); /* 金色边框 */
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(15, 16, 22, 0.8);
  box-shadow: 0 0 8px rgba(251, 191, 36, 0.3);
}

.avatar-text {
  font-family: 'Cinzel', serif;
  font-weight: 700;
  color: var(--text-primary);
}

.logout-btn {
  font-size: 0.9rem;
  padding: 0.5rem 1rem;
}

/* 核心区域 */
.hero-section {
  height: 100vh;
  padding-top: 64px;
  position: relative;
  z-index: 10;
}

.main-title {
  font-family: 'Cinzel', serif;
  font-size: 5rem;
  font-weight: 900;
  letter-spacing: 0.5rem;
  margin-bottom: 1rem;
  text-transform: uppercase;
  /* 金色渐变文字 */
  background: linear-gradient(180deg, #fbbf24 0%, #d97706 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  filter: drop-shadow(0 4px 12px rgba(217, 119, 6, 0.3));
}

.subtitle {
  font-size: 1.25rem;
  letter-spacing: 0.2rem;
  margin-bottom: 3rem;
  font-family: 'Inter', sans-serif;
  text-transform: uppercase;
}

.btn-large {
  padding: 1rem 3rem;
  font-size: 1.25rem;
  font-family: 'Cinzel', serif;
  font-weight: 700;
  letter-spacing: 2px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.btn-large .icon {
  font-size: 1.5rem;
}

/* 按钮光晕特效 */
.glow-effect {
  box-shadow: 0 0 20px rgba(124, 58, 237, 0.4);
  transition: all 0.3s ease;
}

.glow-effect:hover {
  box-shadow: 0 0 30px rgba(124, 58, 237, 0.7);
  transform: translateY(-2px);
}

/* 背景装饰 */
.bg-overlay {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  /* 兜底背景色（深紫色渐变），防止图片加载失败时一片黑 */
  background: linear-gradient(135deg, #0f1016 0%, #1e1b4b 100%);
  z-index: 1;
}

/* 如果图片加载成功，覆盖渐变 */
.bg-overlay::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  /* Unsplash 中世纪/奇幻/神秘风格背景图 */
  background-image: url('https://images.unsplash.com/photo-1519074069444-1ba4fff66d16?q=80&w=2070&auto=format&fit=crop');
  background-size: cover;
  background-position: center;
  /* 初始滤镜 */
  filter: brightness(0.5) blur(1px) sepia(0.2);
  /* 增强动画：缩短周期，增加变化幅度 */
  animation: breathe 15s infinite alternate ease-in-out;
}

/* 叠加一层渐变，增强文字可读性 */
.bg-overlay::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  /* 调整遮罩透明度，让背景更透亮一点 */
  background: radial-gradient(circle at center, rgba(15, 16, 22, 0.2) 0%, rgba(15, 16, 22, 0.8) 100%);
  pointer-events: none;
  z-index: 2;
}

@keyframes breathe {
  0% {
    transform: scale(1);
    filter: brightness(0.5) blur(1px) sepia(0.2);
  }
  100% {
    transform: scale(1.15);
    /* 放大时略微变亮且模糊减少，模拟聚焦感 */
    filter: brightness(0.7) blur(0px) sepia(0.4);
  }
}

.action-area {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1rem;
}

/* 移除原有的 mt-4，统一用 gap 控制间距 */
.action-area .mt-4 {
  margin-top: 0;
}
</style>
