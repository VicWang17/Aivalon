<template>
  <div class="game-container">
    <!-- 顶部状态栏 -->
    <header class="game-header glass-panel">
      <div class="header-left">
        <div class="game-info">
          <span class="label">轮次</span>
          <span class="value">{{ gameState?.round || 1 }}/5</span>
        </div>
        <div class="game-info">
          <span class="label">投票失败</span>
          <span class="value">{{ gameState?.vote_track || 0 }}/5</span>
        </div>
      </div>
      
      <div class="header-center">
        <h2 class="phase-title">{{ currentPhaseText }}</h2>
      </div>

      <div class="header-right">
        <button class="btn-ghost btn-sm" @click="router.push('/')" title="返回主页">
          <span class="icon">🏠</span>
        </button>
      </div>
    </header>

    <!-- 游戏主区域：圆桌与聊天 -->
    <div class="game-main-layout">
      <main class="game-board">
        <div class="round-table">
          <div class="table-center">
            <div class="logo-mark">Aivalon</div>
          </div>
          
          <!-- 玩家座位 -->
          <div 
            v-for="(player, index) in players" 
            :key="player.user_id"
            class="player-seat"
            :class="getPlayerClasses(player)"
            :style="getSeatStyle(index, players.length)"
            @click="toggleSelection(player.user_id)"
          >
            <div class="avatar-wrapper">
              <div class="avatar">
                {{ player.username.charAt(0).toUpperCase() }}
              </div>
              <!-- 状态标记 -->
              <div class="badges">
                <span v-if="player.user_id === gameState?.leader_id" class="badge leader" title="队长">👑</span>
                <span v-if="player.user_id === gameState?.speaker_id" class="badge speaker" title="正在发言">🎙️</span>
                <!-- <span v-if="gameState?.phase === GamePhase.VOTE && player.has_voted" class="badge voted" title="已投票">🗳️</span> -->
                <span v-if="gameState?.phase !== GamePhase.VOTE && voteResultMap[player.user_id] === VoteOption.APPROVE" class="badge voted-approve" title="同意">🟢</span>
                <span v-if="gameState?.phase !== GamePhase.VOTE && voteResultMap[player.user_id] === VoteOption.REJECT" class="badge voted-reject" title="反对">🔴</span>
              </div>
            </div>
            <div class="player-info">
              <span class="name">{{ player.username }}</span>
              <span class="seat-num">#{{ player.seat_id + 1 }}</span>
            </div>
            
            <!-- 角色卡片 (仅自己或特定视角可见) -->
            <div v-if="player.character" class="character-tag" :class="getCharacterClass(player.character)">
              {{ player.character }}
            </div>
            <div v-else-if="player.is_seen_as_merlin" class="character-tag text-merlin" title="可能是梅林或莫甘娜">
              MERLIN?
            </div>
            <div v-else-if="player.is_seen_as_evil" class="character-tag text-evil" title="已知是坏人">
              EVIL
            </div>
          </div>
        </div>
      </main>

      <!-- 聊天窗口 -->
      <aside class="chat-panel glass-panel">
        <div class="chat-header">
          <h3>会议记录</h3>
        </div>
        <div class="chat-history" ref="chatHistoryRef">
          <div v-if="gameState?.speech_history?.length === 0" class="empty-tip">暂无发言</div>
          <div 
            v-for="(msg, idx) in gameState?.speech_history" 
            :key="idx"
            class="chat-message"
            :class="{ 
              'self': msg.user_id === userStore.userInfo?.id,
              'system': msg.user_id === 0
            }"
          >
            <div class="msg-meta">
              <span class="msg-user">{{ msg.username }}</span>
              <!-- <span class="msg-time">{{ formatTime(msg.timestamp) }}</span> -->
            </div>
            <div class="msg-content">
              <span>{{ msg.content }}</span>
            </div>
          </div>
        </div>
        
        <div class="chat-input-area" v-if="gameState?.phase === GamePhase.SPEECH">
          <div class="input-wrapper">
            <textarea 
              v-model="speechInput" 
              :placeholder="isMyTurn ? '请输入发言内容(至少5个字)... (Enter 发送，Ctrl+Enter 结束)' : `等待 ${getPlayerName(gameState.speaker_id || 0)} 发言...`" 
              :disabled="!isMyTurn"
              @keydown.enter.exact.prevent="handleSendSpeech(false)"
              @keydown.ctrl.enter.prevent="handleSendSpeech(true)"
              @keydown.meta.enter.prevent="handleSendSpeech(true)"
            ></textarea>
            <div class="input-actions">
              <button 
                class="btn-primary btn-sm" 
                @click="handleSendSpeech(true)" 
                :disabled="!isMyTurn || speechInput.length < 5"
              >
                发送并结束发言
              </button>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <!-- 底部任务进度与操作区 -->
    <footer class="game-footer glass-panel">
      <!-- 任务进度 -->
      <div class="mission-track">
        <div 
          v-for="i in 5" 
          :key="i" 
          class="mission-node"
          :class="getMissionClass(i)"
        >
          <span class="mission-num">{{ getMissionSize(i) }}</span>
        </div>
      </div>

      <!-- 操作区 -->
      <div class="action-bar">
        <div v-if="isMyTurn" class="my-turn-actions">
          <!-- 发言阶段 (已移至聊天窗口) -->
          
          <!-- 提名阶段 -->
          <div v-if="gameState?.phase === GamePhase.TEAM_PROPOSAL" class="propose-actions">
            <div class="action-tip">
              请选择 {{ requiredTeamSize }} 名玩家执行任务 (已选 {{ selectedPlayerIds.length }})
            </div>
            <button 
              class="btn-primary" 
              @click="handleAction(ActionType.PROPOSE, { target_ids: selectedPlayerIds })"
              :disabled="selectedPlayerIds.length !== requiredTeamSize"
            >
              确认提名
            </button>
          </div>
          
          <!-- 投票阶段 -->
          <div v-if="gameState?.phase === GamePhase.VOTE" class="vote-buttons">
            <button class="btn-success" @click="handleAction(ActionType.VOTE, { option: VoteOption.APPROVE })">同意</button>
            <button class="btn-danger" @click="handleAction(ActionType.VOTE, { option: VoteOption.REJECT })">反对</button>
          </div>
          
           <!-- 任务阶段 -->
          <div v-if="gameState?.phase === GamePhase.MISSION" class="mission-buttons">
            <button class="btn-success" @click="handleAction(ActionType.MISSION, { result: MissionResult.SUCCESS })">成功</button>
            <button class="btn-danger" @click="handleAction(ActionType.MISSION, { result: MissionResult.FAIL })">失败</button>
          </div>

          <!-- 刺杀阶段 -->
          <div v-if="gameState?.phase === GamePhase.ASSASSINATION" class="assassin-actions">
            <span class="action-tip">请点击场上玩家头像进行刺杀 (已选: {{ getPlayerName(selectedPlayerIds[0] || 0) }})</span>
            <button 
              class="btn-danger" 
              @click="handleAction(ActionType.ASSASSINATE, { target_id: selectedPlayerIds[0] })"
              :disabled="selectedPlayerIds.length !== 1"
            >
              确认刺杀
            </button>
          </div>
        </div>
        <div v-else-if="gameState?.phase === GamePhase.FINISHED" class="game-over-actions">
          <div class="result-text">
            游戏结束，{{ gameState.winner === 'good' ? '好人' : '坏人' }} 阵营胜利！
          </div>
          <button class="btn-primary" @click="router.push('/')">
            返回首页
          </button>
        </div>
        <div v-else class="waiting-text">
          等待其他玩家操作...
        </div>
      </div>
    </footer>
    
    <!-- 背景层 -->
    <div class="bg-overlay"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed, onUnmounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getGameState, submitAction } from '../api/game'
import { v4 as uuidv4 } from 'uuid'
import { useUserStore } from '../store/user'
import type { GameState, PlayerState } from '../types/api'
import { GamePhase, Character, MissionResult, ActionType, VoteOption } from '../types/api'
import { GameSocket } from '../utils/socket'
import { WebSocketOpCode } from '../types/protocol'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const gameId = route.params.gameId as string
const gameState = ref<GameState | null>(null)
const loading = ref(false)
const currentActionIdempotencyKey = ref<string>('')
const speechInput = ref('')
const selectedPlayerIds = ref<number[]>([])
const chatHistoryRef = ref<HTMLElement | null>(null)
let socket: GameSocket | null = null

// 监听 speech_history 变化
watch(() => gameState.value?.speech_history, (newVal, oldVal) => {
  if (!newVal) return
  
  // 找出新增的消息
  const oldLen = oldVal ? oldVal.length : 0
  if (newVal.length > oldLen) {
    // 滚动到底部
    nextTick(() => {
      scrollToBottom()
    })
  }
}, { deep: true })

const scrollToBottom = () => {
  if (chatHistoryRef.value) {
    chatHistoryRef.value.scrollTop = chatHistoryRef.value.scrollHeight
  }
}


// 计算属性
const players = computed(() => gameState.value?.players || [])

// Basic lookup table for standard Avalon
const MISSION_CONFIG: Record<number, Record<number, number>> = {
  5: { 1: 2, 2: 3, 3: 2, 4: 3, 5: 3 },
  6: { 1: 2, 2: 3, 3: 4, 4: 3, 5: 4 },
  7: { 1: 2, 2: 3, 3: 3, 4: 4, 5: 4 },
  8: { 1: 3, 2: 4, 3: 4, 4: 5, 5: 5 },
  9: { 1: 3, 2: 4, 3: 4, 4: 5, 5: 5 },
  10: { 1: 3, 2: 4, 3: 4, 4: 5, 5: 5 },
}

const requiredTeamSize = computed(() => {
  if (!gameState.value) return 0
  const playerCount = gameState.value.players.length
  const round = gameState.value.round
  
  return MISSION_CONFIG[playerCount]?.[round] || 0
})

const getMissionSize = (round: number) => {
  if (!gameState.value) return round
  const playerCount = gameState.value.players.length
  return MISSION_CONFIG[playerCount]?.[round] || round
}

const voteResultMap = computed(() => {
  if (!gameState.value?.votes) return {}
  return gameState.value.votes
})

const isMyTurn = computed(() => {
  if (!gameState.value || !userStore.userInfo) return false
  const myId = userStore.userInfo.id
  const g = gameState.value
  
  switch (g.phase) {
    case GamePhase.SPEECH:
      return g.speaker_id === myId
    case GamePhase.TEAM_PROPOSAL:
      return g.leader_id === myId
    case GamePhase.VOTE:
      // 检查我是否还没投票
      const me = g.players.find(p => p.user_id === myId)
      return me && !me.has_voted
    case GamePhase.MISSION:
      // 检查我是否在队伍里且没执行
      const inTeam = g.proposed_team.includes(myId)
      const myState = g.players.find(p => p.user_id === myId)
      return inTeam && myState && !myState.has_acted
    case GamePhase.ASSASSINATION:
      // 检查我是否是刺客
      const myChar = g.players.find(p => p.user_id === myId)?.character
      return myChar === Character.ASSASSIN
    default:
      return false
  }
})

const currentPhaseText = computed(() => {
  const map: Record<string, string> = {
    [GamePhase.LEADER_SELECTION]: '选队长',
    [GamePhase.SPEECH]: '发言阶段',
    [GamePhase.TEAM_PROPOSAL]: '组队阶段',
    [GamePhase.VOTE]: '投票阶段',
    [GamePhase.MISSION]: '任务执行',
    [GamePhase.ASSASSINATION]: '刺杀时刻',
    [GamePhase.FINISHED]: '游戏结束'
  }
  return gameState.value ? map[gameState.value.phase] || gameState.value.phase : '加载中...'
})

// 方法
const fetchGameState = async () => {
  try {
    const res = await getGameState(gameId)
    // 兼容多种响应结构: ResponseModel 或 AxiosResponse
    const responseData = (res as any).data || res
    const realData = (responseData.code === 0 && responseData.data) ? responseData.data : responseData
    gameState.value = realData
  } catch (error) {
    console.error('Fetch game failed', error)
  }
}

const getPlayerName = (id: number) => {
  return gameState.value?.players.find(p => p.user_id === id)?.username || 'Unknown'
}

const handleSendSpeech = async (isEnd: boolean) => {
  if (!speechInput.value.trim() || speechInput.value.trim().length < 5) return
  
  await handleAction(ActionType.SPEAK, {
    content: speechInput.value,
    is_end: isEnd
  })
  
  if (!isEnd) {
    speechInput.value = ''
  } else {
    speechInput.value = '' // Clear on end too
  }
}

const handleAction = async (type: string, payload: any = {}) => {
  if (loading.value) return
  loading.value = true

  // 生成新的幂等键
  currentActionIdempotencyKey.value = uuidv4()

  try {
    const res = await submitAction(gameId, {
      action_type: type as any,
      payload
    }, currentActionIdempotencyKey.value)
    // 兼容多种响应结构
    const responseData = (res as any).data || res
    const realData = (responseData.code === 0 && responseData.data) ? responseData.data : responseData
    gameState.value = realData
    // 动作提交后立即刷新一次状态
  } catch (error) {
    console.error('Action failed', error)
    if ((error as any).response?.status === 409) {
      console.warn('Duplicate request detected, refreshing state...')
      await fetchGameState()
    } else {
      const msg = (error as any).response?.data?.detail || (error as any).message || '未知错误'
      alert('操作失败: ' + msg)
    }
  } finally {
    loading.value = false
  }
}

const toggleSelection = (userId: number) => {
  // 仅在提名阶段且是队长时允许选择
  const isProposalPhase = gameState.value?.phase === GamePhase.TEAM_PROPOSAL && gameState.value?.leader_id === userStore.userInfo?.id
  const isAssassinationPhase = gameState.value?.phase === GamePhase.ASSASSINATION && isMyTurn.value
  
  if (!isProposalPhase && !isAssassinationPhase) return
  
  const index = selectedPlayerIds.value.indexOf(userId)
  if (index > -1) {
    selectedPlayerIds.value.splice(index, 1)
  } else {
    // Check if limit reached
    const limit = isAssassinationPhase ? 1 : requiredTeamSize.value
    
    if (selectedPlayerIds.value.length >= limit) {
        // 如果是刺杀阶段，选择新的目标时自动替换旧目标
        if (isAssassinationPhase) {
          selectedPlayerIds.value = [userId]
          return
        }
        // alert(`本轮任务只能选择 ${limit} 名玩家`)
        return
    }
    selectedPlayerIds.value.push(userId)
  }
}

// 辅助样式方法
const getSeatStyle = (index: number, total: number) => {
  const angle = (360 / total) * index - 90 // -90 让第一个人在顶部
  const radius = 260 // 半径 px
  return {
    transform: `rotate(${angle}deg) translate(${radius}px) rotate(${-angle}deg) translate(-50%, -50%)`
  }
}

const getPlayerClasses = (player: PlayerState) => {
  return {
    'is-me': player.user_id === userStore.userInfo?.id,
    'is-leader': player.user_id === gameState.value?.leader_id,
    'in-team': gameState.value?.proposed_team.includes(player.user_id),
    'is-selected': selectedPlayerIds.value.includes(player.user_id)
  }
}

const getCharacterClass = (char: string) => {
  const evilChars = [Character.MORGANA, Character.ASSASSIN, Character.MINION, Character.MORDRED, Character.OBERON]
  return evilChars.includes(char as any) ? 'text-evil' : 'text-good'
}

const getMissionClass = (index: number) => {
  if (!gameState.value) return ''
  const results = gameState.value.mission_results
  // index is 1-based, results is 0-based
  if (index <= results.length) {
    return results[index - 1] === MissionResult.SUCCESS ? 'mission-success' : 'mission-fail'
  }
  if (index === gameState.value.round) return 'mission-current'
  return ''
}

// 监听游戏状态变化
watch(
  () => [gameState.value?.phase, gameState.value?.round],
  ([newPhase, newRound], [oldPhase, oldRound]) => {
    if (newPhase !== oldPhase || newRound !== oldRound) {
      selectedPlayerIds.value = []
    }
  }
)

// 生命周期
onMounted(async () => {
  if (!userStore.userInfo && userStore.token) {
    await userStore.fetchUserInfo()
  }
  fetchGameState()
  
  if (gameId) {
    socket = new GameSocket(gameId)
    socket.onMessage((msg) => {
      // 简单处理：收到状态更新或快照时，刷新页面状态
      if (msg.type === WebSocketOpCode.STATE_UPDATE || msg.type === WebSocketOpCode.GAME_SNAPSHOT) {
        fetchGameState()
      }
    })
    socket.connect()
  }
})

onUnmounted(() => {
  if (socket) {
    socket.disconnect()
  }
})
</script>

<style scoped>
.game-container {
  height: 100vh;
  background: transparent;
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

/* Header */
.game-header {
  position: relative;
  z-index: 10;
  height: 60px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 1.5rem;
  z-index: 10;
  border-bottom: 1px solid rgba(255,255,255,0.1);
  background: rgba(0, 0, 0, 0.2);
}

.header-left {
  display: flex;
  gap: 1.5rem;
}

.game-info {
  display: flex;
  flex-direction: column;
  font-size: 0.8rem;
}

.game-info .label {
  color: var(--text-secondary);
}

.game-info .value {
  font-family: 'Cinzel', serif;
  font-weight: bold;
  color: var(--text-accent);
}

.header-center {
  text-align: center;
}

.phase-title {
  font-size: 1.2rem;
  font-family: 'Cinzel', serif;
  margin: 0;
  color: var(--text-primary);
}

/* Layout */
.game-main-layout {
  position: relative;
  z-index: 10;
  flex: 1;
  display: flex;
  gap: 20px;
  padding: 20px;
  width: 100%;
  position: relative;
  overflow: hidden; /* 防止主布局本身滚动 */
  min-height: 0; /* 允许 flex item 压缩 */
}

/* Board */
.game-board {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  /* Background */
  background: transparent;
  border-radius: 12px;
}

.round-table {
  width: 600px;
  height: 600px;
  position: relative;
  border-radius: 50%;
  border: 2px solid rgba(251, 191, 36, 0.1); /* 金色微光 */
}

.table-center {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 200px;
  height: 200px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.03);
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(255, 255, 255, 0.05);
}

.logo-mark {
  font-family: 'Cinzel', serif;
  font-size: 2rem;
  color: rgba(255, 255, 255, 0.1);
  letter-spacing: 0.2rem;
}

/* Chat Panel */
.chat-panel {
  width: 400px;
  height: 100%;
  margin-right: 40px;
  display: flex;
  flex-direction: column;
  background: rgba(30, 30, 40, 0.4);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 12px;
  overflow: hidden;
  backdrop-filter: blur(10px);
  min-height: 0; /* 确保在 flex 容器中能够正确收缩 */
}

.chat-header {
  flex-shrink: 0;
  padding: 12px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(0, 0, 0, 0.2);
}

.chat-header h3 {
  margin: 0;
  font-size: 16px;
  color: var(--text-primary);
}

.chat-history {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0; /* 确保滚动容器正确收缩 */
}

/* Custom Scrollbar */
.chat-history::-webkit-scrollbar {
  width: 6px;
}

.chat-history::-webkit-scrollbar-track {
  background: rgba(0, 0, 0, 0.2);
  border-radius: 3px;
}

.chat-history::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.2);
  border-radius: 3px;
}

.chat-history::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.3);
}

.empty-tip {
  text-align: center;
  color: var(--text-secondary);
  margin-top: 20px;
  font-size: 14px;
}

.chat-message {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-width: 85%;
  align-self: flex-start;
}

.chat-message.self {
  align-self: flex-end;
}

.chat-message.system {
  align-self: center;
  max-width: 95%;
  margin: 8px 0;
}

.chat-message.system .msg-content {
  background: rgba(124, 58, 237, 0.2); /* 淡淡的紫色背景 */
  border: 1px solid rgba(124, 58, 237, 0.4);
  color: #e2e8f0;
  text-align: left;
  white-space: pre-wrap; /* 保留换行符 */
}

.chat-message.system .msg-meta {
  justify-content: center;
  color: var(--text-accent);
  font-weight: bold;
}

.msg-meta {
  font-size: 12px;
  color: var(--text-secondary);
  display: flex;
  gap: 8px;
}

.self .msg-meta {
  justify-content: flex-end;
}

.msg-content {
  background: rgba(255, 255, 255, 0.1);
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.4;
  color: var(--text-primary);
  border-top-left-radius: 2px;
  white-space: pre-wrap;
  word-break: break-word;
}

.self .msg-content {
  background: var(--color-primary);
  color: #fff;
  border-top-left-radius: 8px;
  border-top-right-radius: 2px;
}

.chat-input-area {
  padding: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(0, 0, 0, 0.2);
}

.input-wrapper {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.input-wrapper textarea {
  width: 100%;
  height: 60px;
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 6px;
  padding: 8px;
  color: var(--text-primary);
  resize: none;
  font-family: inherit;
}

.input-wrapper textarea:focus {
  outline: none;
  border-color: var(--color-primary);
}

.input-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.waiting-tip {
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
  padding: 10px 0;
  font-style: italic;
}

/* Player Seats */
.player-seat {
  position: absolute;
  top: 50%;
  left: 50%;
  /* width/height controlled by children */
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  transform-origin: center;
}

.avatar-wrapper {
  position: relative;
}

.avatar {
  width: 60px;
  height: 60px;
  border-radius: 50%;
  background: #2d3748;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.5rem;
  font-weight: bold;
  color: #fff;
  border: 2px solid #4a5568;
  transition: all 0.3s ease;
}

.player-seat.is-me .avatar {
  border-color: var(--color-primary);
  box-shadow: 0 0 15px rgba(168, 85, 247, 0.3);
}

.player-seat.is-leader .avatar {
  border-color: var(--color-accent);
}

.player-seat.in-team .avatar {
  border-color: #10b981; /* Green */
  box-shadow: 0 0 10px rgba(16, 185, 129, 0.4);
}

.player-seat.is-selected .avatar {
  border-color: #fbbf24;
  box-shadow: 0 0 15px rgba(251, 191, 36, 0.6);
  transform: scale(1.15);
  cursor: pointer;
}

.badges {
  position: absolute;
  top: -10px;
  right: -10px;
  display: flex;
  gap: 4px;
}

.badge {
  font-size: 1rem;
  background: rgba(0,0,0,0.5);
  border-radius: 50%;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.player-info {
  text-align: center;
  display: flex;
  flex-direction: column;
}

.player-info .name {
  font-size: 0.9rem;
  font-weight: 500;
}

.player-info .seat-num {
  font-size: 0.7rem;
  color: var(--text-secondary);
}

.character-tag {
  font-size: 0.8rem;
  padding: 2px 8px;
  background: rgba(0,0,0,0.6);
  border-radius: 4px;
  margin-top: 4px;
}

.text-good { color: #60a5fa; }
.text-evil { color: #f87171; }
.text-merlin { color: #a78bfa; } /* Purple for Merlin/Morgana */

/* Footer */
.game-footer {
  position: relative;
  z-index: 10;
  height: 80px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0 24px;
  border-top: 1px solid rgba(255,255,255,0.1);
  background: rgba(0,0,0,0.2);
}

/* Buttons */
.btn-primary {
  background: var(--color-primary);
  color: white;
  border: none;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
  transition: background 0.2s;
}
.btn-primary:hover {
  background: var(--color-primary-hover);
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-warning {
  background: var(--color-warning);
  color: #1a1b26;
  border: none;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
}
.btn-sm {
  padding: 4px 12px;
  font-size: 0.85rem;
}


.mission-track {
  display: flex;
  gap: 1rem;
}

.mission-node {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: 2px solid #475569;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.9rem;
  color: #94a3b8;
  background: rgba(0,0,0,0.3);
}

.mission-success {
  background: #3b82f6;
  border-color: #60a5fa;
  color: white;
}

.mission-fail {
  background: #ef4444;
  border-color: #f87171;
  color: white;
}

.mission-current {
  border-color: var(--text-accent);
  box-shadow: 0 0 10px rgba(251, 191, 36, 0.3);
}

.action-bar {
  min-height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.my-turn-actions {
  display: flex;
  gap: 1rem;
}

.waiting-text {
  color: var(--text-secondary);
  font-style: italic;
  font-size: 0.9rem;
}

/* Buttons */
.btn-sm { padding: 0.25rem 0.5rem; }
.btn-success {
  background: #10b981;
  color: white;
  border: none;
  padding: 0.5rem 1.5rem;
  border-radius: 4px;
  cursor: pointer;
}
.btn-danger {
  background: #ef4444;
  color: white;
  border: none;
  padding: 0.5rem 1.5rem;
  border-radius: 4px;
  cursor: pointer;
}

/* Background */
.bg-overlay {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background-image: url('../assets/images/game_bg.png');
  background-size: cover;
  background-position: center;
  z-index: 1;
}

.bg-overlay::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: radial-gradient(circle at center, rgba(15, 16, 22, 0.4) 0%, rgba(15, 16, 22, 0.8) 100%);
  backdrop-filter: blur(2px); /* 降低模糊度让背景更清晰 */
}
</style>