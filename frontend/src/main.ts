// 这个文件是Vue前端应用的入口文件，负责创建Vue应用实例、挂载根组件和引入全局样式。
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import './style.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.mount('#app')
