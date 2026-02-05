// 这个文件封装了 Token 的存储、获取和移除操作，目前使用 localStorage。
const TokenKey = 'aivalon_token'

export function getToken() {
  return localStorage.getItem(TokenKey)
}

export function setToken(token: string) {
  return localStorage.setItem(TokenKey, token)
}

export function removeToken() {
  return localStorage.removeItem(TokenKey)
}
