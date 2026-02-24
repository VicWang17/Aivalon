<template>
  <div class="replay-container page-container">
    <nav class="top-nav glass-panel">
      <div class="nav-left">
        <router-link to="/history" class="nav-back">← 返回历史</router-link>
        <span class="nav-title">对局回放 (ID: {{ gameId?.slice(0, 8) }})</span>
      </div>
      <div class="nav-right">
        <span class="replay-status">{{ isPlaying ? '播放中' : '暂停' }}</span>
      </div>
    </nav>

    <main class="content-area">
      <!-- 进度条 -->
      <div class="timeline-control glass-panel">
        <div class="progress-bar-wrapper">
          <input 
            type="range" 
            min="0" 
            :max="totalEvents - 1" 
            v-model.number="currentIndex"
            @input="handleSeek"
            class="timeline-slider"
          >
        </div>
        <div class="controls">
          <button @click="togglePlay" class="btn-icon">
            {{ isPlaying ? '⏸' : '▶' }}
          </button>
          <span class="step-info">{{ currentIndex + 1 }} / {{ totalEvents }}</span>
        </div>
      </div>

      <!-- 当前事件展示 -->
      <div class="event-display glass-panel" v-if="currentEvent">
        <div class="event-header">
          <span class="event-seq">#{{ currentEvent.seq }}</span>
          <span class="event-type">{{ formatEventType(currentEvent.event_type) }}</span>
          <span class="event-time">{{ formatTime(currentEvent.created_at) }}</span>
        </div>
        
        <div class="event-body">
          <div class="payload-viewer">
            <pre>{{ JSON.stringify(currentEvent.payload, null, 2) }}</pre>
          </div>
        </div>
        
        <div class="event-player" v-if="currentEvent.player_id">
          <span class="label">操作玩家:</span>
          <span class="value">{{ currentEvent.player_id }}</span>
        </div>
      </div>

      <!-- 事件列表 -->
      <div class="event-list glass-panel">
        <div 
          v-for="(event, index) in events" 
          :key="event.id"
          :class="['event-item', { active: index === currentIndex }]"
          @click="jumpTo(index)"
        >
          <span class="seq">{{ event.seq }}</span>
          <span class="type">{{ formatEventType(event.event_type) }}</span>
          <span class="summary">{{ getEventSummary(event) }}</span>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { getGameEvents } from '../api/game'
import type { GameEvent } from '../types/api'
import { ActionType } from '../types/api'

const route = useRoute()
const gameId = computed(() => route.params.gameId as string)

const events = ref<GameEvent[]>([])
const currentIndex = ref(0)
const isPlaying = ref(false)
let playTimer: number | null = null

const totalEvents = computed(() => events.value.length)
const currentEvent = computed(() => events.value[currentIndex.value] || null)

onMounted(async () => {
  if (!gameId.value) return
  try {
    const res = await getGameEvents(gameId.value)
    events.value = Array.isArray(res) ? res : (res as any).data || []
  } catch (error) {
    console.error('Failed to load events', error)
  }
})

onUnmounted(() => {
  stopPlay()
})

const togglePlay = () => {
  if (isPlaying.value) {
    stopPlay()
  } else {
    startPlay()
  }
}

const startPlay = () => {
  if (currentIndex.value >= totalEvents.value - 1) {
    currentIndex.value = 0
  }
  isPlaying.value = true
  playTimer = window.setInterval(() => {
    if (currentIndex.value < totalEvents.value - 1) {
      currentIndex.value++
    } else {
      stopPlay()
    }
  }, 1000) // 1秒一步
}

const stopPlay = () => {
  isPlaying.value = false
  if (playTimer) {
    clearInterval(playTimer)
    playTimer = null
  }
}

const handleSeek = () => {
  stopPlay()
}

const jumpTo = (index: number) => {
  currentIndex.value = index
  stopPlay()
}

const formatEventType = (type: string) => {
  const map: Record<string, string> = {
    'GAME_START': '游戏开始',
    [ActionType.PROPOSE]: '提名',
    [ActionType.VOTE]: '投票',
    [ActionType.MISSION]: '执行任务',
    [ActionType.ASSASSINATE]: '刺杀',
    [ActionType.SPEAK]: '发言',
    'STATE_UPDATE': '状态更新'
  }
  return map[type] || type
}

const formatTime = (timeStr: string) => {
  return new Date(timeStr).toLocaleTimeString()
}

const getEventSummary = (event: GameEvent) => {
  if (event.event_type === ActionType.SPEAK) {
    return event.payload.content?.slice(0, 20) + '...'
  }
  if (event.event_type === ActionType.PROPOSE) {
    return `提议队伍: ${event.payload.proposed_team}`
  }
  return JSON.stringify(event.payload).slice(0, 30)
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
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.timeline-control {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.progress-bar-wrapper {
  width: 100%;
}

.timeline-slider {
  width: 100%;
  accent-color: var(--accent-primary);
}

.controls {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.btn-icon {
  background: none;
  border: 1px solid var(--text-secondary);
  color: var(--text-primary);
  border-radius: 50%;
  width: 32px;
  height: 32px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.btn-icon:hover {
  background: rgba(255, 255, 255, 0.1);
  border-color: var(--accent-primary);
}

.event-display {
  padding: 20px;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.event-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  padding-bottom: 8px;
}

.event-seq {
  font-weight: bold;
  color: var(--accent-secondary);
}

.payload-viewer {
  background: rgba(0, 0, 0, 0.3);
  padding: 12px;
  border-radius: 4px;
  overflow-x: auto;
}

pre {
  margin: 0;
  font-family: monospace;
  font-size: 0.85em;
  color: #a8d1ff;
}

.event-list {
  max-height: 400px;
  overflow-y: auto;
  padding: 0;
}

.event-item {
  display: grid;
  grid-template-columns: 40px 100px 1fr;
  padding: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  cursor: pointer;
  transition: background 0.2s;
}

.event-item:hover {
  background: rgba(255, 255, 255, 0.05);
}

.event-item.active {
  background: rgba(193, 168, 117, 0.2); /* Gold tint */
  border-left: 3px solid var(--accent-primary);
}

.nav-back {
  color: var(--text-primary);
  text-decoration: none;
  margin-right: 16px;
}
</style>