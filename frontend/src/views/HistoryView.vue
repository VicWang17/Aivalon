<template>
  <div class="history-container page-container">
    <nav class="top-nav glass-panel">
      <div class="nav-left">
        <router-link to="/" class="nav-back">← 返回</router-link>
        <span class="nav-title">对局历史</span>
      </div>
    </nav>

    <main class="content-area">
      <div v-if="loading" class="loading-state">
        <div class="spinner"></div>
        <p>加载中...</p>
      </div>
      
      <div v-else-if="games.length === 0" class="empty-state">
        <p>暂无对局记录</p>
        <router-link to="/" class="btn-primary">开始第一局</router-link>
      </div>

      <div v-else class="history-list">
        <div 
          v-for="game in games" 
          :key="game.id" 
          class="history-card glass-panel"
          @click="goToReplay(game.id)"
        >
          <div class="card-header">
            <span class="game-time">{{ formatDate(game.created_at) }}</span>
            <span :class="['status-badge', game.status]">
              {{ formatStatus(game.status) }}
            </span>
          </div>
          
          <div class="card-body">
            <div class="info-row">
              <span class="label">对局 ID:</span>
              <span class="value">{{ game.id.slice(0, 8) }}...</span>
            </div>
            <div class="info-row">
              <span class="label">获胜阵营:</span>
              <span :class="['value', 'winner', game.winner || 'unknown']">
                {{ formatWinner(game.winner) }}
              </span>
            </div>
            <div class="info-row">
              <span class="label">玩家人数:</span>
              <span class="value">{{ game.player_ids.length }} 人</span>
            </div>
          </div>

          <div class="card-footer">
            <span class="hint">点击查看回放 →</span>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getGameHistory } from '../api/game'
import type { GameSummary } from '../types/api'

const router = useRouter()
const games = ref<GameSummary[]>([])
const loading = ref(true)

onMounted(async () => {
  try {
    const res = await getGameHistory()
    // 兼容处理：有些 request 封装会直接返回 data，有些返回 response
    games.value = Array.isArray(res) ? res : (res as any).data || []
  } catch (error) {
    console.error('Failed to fetch history', error)
  } finally {
    loading.value = false
  }
})

const goToReplay = (gameId: string) => {
  router.push(`/replay/${gameId}`)
}

const formatDate = (dateStr: string) => {
  return new Date(dateStr).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

const formatStatus = (status: string) => {
  const map: Record<string, string> = {
    'playing': '进行中',
    'finished': '已结束',
    'created': '等待中'
  }
  return map[status] || status
}

const formatWinner = (winner: string | null) => {
  if (!winner) return '未决出'
  const map: Record<string, string> = {
    'good': '蓝方 (正义)',
    'evil': '红方 (邪恶)'
  }
  return map[winner] || winner
}
</script>

<style scoped>
.page-container {
  min-height: 100vh;
  background: var(--bg-primary);
  color: var(--text-primary);
  padding-top: 80px;
}

.content-area {
  max-width: 800px;
  margin: 0 auto;
  padding: 20px;
}

.history-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.history-card {
  padding: 20px;
  cursor: pointer;
  transition: transform 0.2s, box-shadow 0.2s;
  border-left: 4px solid transparent;
}

.history-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
  border-left-color: var(--accent-primary);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.game-time {
  font-family: 'Cinzel', serif;
  color: var(--text-secondary);
}

.status-badge {
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 0.85em;
  background: rgba(255, 255, 255, 0.1);
}

.status-badge.playing { color: var(--accent-secondary); }
.status-badge.finished { color: var(--accent-primary); }

.card-body {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}

.info-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.label {
  font-size: 0.85em;
  color: var(--text-secondary);
}

.value {
  font-weight: 500;
}

.value.winner.good { color: #4a90e2; }
.value.winner.evil { color: #e24a4a; }

.card-footer {
  margin-top: 12px;
  text-align: right;
  font-size: 0.85em;
  color: var(--accent-primary);
  opacity: 0;
  transition: opacity 0.2s;
}

.history-card:hover .card-footer {
  opacity: 1;
}

.loading-state, .empty-state {
  text-align: center;
  padding: 40px;
  color: var(--text-secondary);
}

.nav-back {
  color: var(--text-primary);
  text-decoration: none;
  margin-right: 16px;
  font-weight: bold;
}
</style>