<template>
  <div class="leaderboard-container">
    <div class="header">
      <h1>🏆 胜场排行</h1>
      <div class="tabs">
        <button 
          :class="{ active: currentTab === 'total' }" 
          @click="switchTab('total')"
        >
          总榜
        </button>
        <button 
          :class="{ active: currentTab === 'good' }" 
          @click="switchTab('good')"
        >
          蓝方 (好人)
        </button>
        <button 
          :class="{ active: currentTab === 'evil' }" 
          @click="switchTab('evil')"
        >
          红方 (坏人)
        </button>
      </div>
    </div>

    <div class="table-wrapper" v-loading="loading">
      <table class="leaderboard-table">
        <thead>
          <tr>
            <th width="80">排名</th>
            <th>玩家</th>
            <th width="100">{{ getWinColumnTitle() }}</th>
            <th width="100">总场次</th>
            <th width="100">胜率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(entry, index) in list" :key="entry.user_id" :class="{'my-rank': isMe(entry.user_id)}">
            <td class="rank-cell">
              <div v-if="index < 3" :class="['rank-badge', `rank-${index + 1}`]">
                {{ index + 1 }}
              </div>
              <span v-else class="rank-num">{{ index + 1 }}</span>
            </td>
            <td class="player-cell">
              <span class="username">{{ entry.username }}</span>
              <span v-if="isMe(entry.user_id)" class="me-tag">我</span>
            </td>
            <td class="highlight-val">{{ getWinValue(entry) }}</td>
            <td>{{ entry.total_games }}</td>
            <td>{{ entry.win_rate }}%</td>
          </tr>
          <tr v-if="list.length === 0">
            <td colspan="5" class="empty-text">暂无数据</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getLeaderboard } from '../api/leaderboard'
import type { LeaderboardEntry } from '../types/api'
import { useUserStore } from '../store/user'

const userStore = useUserStore()
const currentTab = ref<'total' | 'good' | 'evil'>('total')
const list = ref<LeaderboardEntry[]>([])
const loading = ref(false)

const switchTab = (type: 'total' | 'good' | 'evil') => {
  currentTab.value = type
  fetchData()
}

const fetchData = async () => {
  loading.value = true
  try {
    const res = await getLeaderboard(currentTab.value)
    list.value = res.data
  } catch (error) {
    console.error(error)
  } finally {
    loading.value = false
  }
}

const getWinColumnTitle = () => {
  if (currentTab.value === 'total') return '总胜场'
  if (currentTab.value === 'good') return '蓝方胜场'
  if (currentTab.value === 'evil') return '红方胜场'
  return '胜场'
}

const getWinValue = (entry: LeaderboardEntry) => {
  if (currentTab.value === 'total') return entry.total_wins
  if (currentTab.value === 'good') return entry.wins_good
  if (currentTab.value === 'evil') return entry.wins_evil
  return 0
}

const isMe = (userId: number) => {
  return userStore.userId === userId
}

onMounted(() => {
  fetchData()
})
</script>

<style scoped>
.leaderboard-container {
  max-width: 800px;
  margin: 2rem auto;
  padding: 0 1rem;
  color: var(--color-text-primary);
}

.header {
  text-align: center;
  margin-bottom: 2rem;
}

h1 {
  font-family: 'Cinzel', serif;
  color: var(--color-gold);
  margin-bottom: 1.5rem;
  text-shadow: 0 2px 4px rgba(0,0,0,0.5);
}

.tabs {
  display: inline-flex;
  background: rgba(0, 0, 0, 0.3);
  padding: 4px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
}

.tabs button {
  background: transparent;
  border: none;
  color: var(--color-text-secondary);
  padding: 0.5rem 1.5rem;
  cursor: pointer;
  border-radius: 6px;
  transition: all 0.3s;
  font-family: 'Cinzel', serif;
  font-size: 0.9rem;
}

.tabs button:hover {
  color: var(--color-text-primary);
}

.tabs button.active {
  background: var(--color-primary);
  color: #fff;
  font-weight: bold;
  box-shadow: 0 2px 4px rgba(0,0,0,0.2);
}

.table-wrapper {
  background: rgba(30, 30, 35, 0.8);
  border-radius: 12px;
  padding: 1rem;
  border: 1px solid var(--color-border);
  box-shadow: 0 8px 16px rgba(0, 0, 0, 0.2);
  backdrop-filter: blur(10px);
}

.leaderboard-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
}

.leaderboard-table th {
  padding: 1rem;
  color: var(--color-text-secondary);
  font-weight: 500;
  text-align: center;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  font-size: 0.9rem;
  text-transform: uppercase;
  letter-spacing: 1px;
}

.leaderboard-table td {
  padding: 1rem;
  text-align: center;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  transition: background 0.2s;
}

.leaderboard-table tr:last-child td {
  border-bottom: none;
}

.leaderboard-table tr:hover td {
  background: rgba(255, 255, 255, 0.02);
}

.rank-cell {
  display: flex;
  justify-content: center;
  align-items: center;
}

.rank-badge {
  width: 28px;
  height: 28px;
  line-height: 28px;
  border-radius: 50%;
  font-weight: bold;
  font-size: 0.9rem;
  box-shadow: 0 2px 4px rgba(0,0,0,0.3);
  color: #1a1a1a;
}

.rank-1 { 
  background: linear-gradient(135deg, #FFD700, #FDB931); 
  border: 1px solid #FFF;
} 
.rank-2 { 
  background: linear-gradient(135deg, #E0E0E0, #B0B0B0); 
  border: 1px solid #FFF;
} 
.rank-3 { 
  background: linear-gradient(135deg, #CD7F32, #A0522D); 
  border: 1px solid #FFF;
}

.rank-num {
  font-family: 'Cinzel', serif;
  font-size: 1.1rem;
  color: var(--color-text-secondary);
}

.player-cell {
  text-align: left;
  padding-left: 2rem !important;
}

.username {
  font-weight: 500;
}

.me-tag {
  display: inline-block;
  font-size: 0.7rem;
  background: var(--color-primary);
  color: white;
  padding: 1px 4px;
  border-radius: 4px;
  margin-left: 8px;
  vertical-align: middle;
}

.my-rank td {
  background: rgba(var(--color-primary-rgb), 0.1);
}

.highlight-val {
  color: var(--color-gold);
  font-weight: bold;
  font-family: 'Cinzel', serif;
  font-size: 1.1rem;
}

.empty-text {
  padding: 3rem;
  color: var(--color-text-secondary);
  font-style: italic;
}
</style>
