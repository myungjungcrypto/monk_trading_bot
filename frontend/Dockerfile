FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# nginx로 정적 파일 서빙
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
# SPA 라우팅용 nginx 설정은 외부 마운트
EXPOSE 80
