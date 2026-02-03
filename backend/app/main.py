# 这个文件是FastAPI应用的入口文件，负责初始化应用实例、配置中间件和路由。
from fastapi import FastAPI

app = FastAPI(
    title="Aivalon",
    description="Aivalon - AI-driven Avalon Game Platform",
    version="0.1.0"
)

@app.get("/")
async def root():
    return {"message": "Welcome to Aivalon"}

@app.get("/health")
async def health_check():
    return {"status": "ok"}
