FROM node:22.20.0-alpine AS build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/angular.json frontend/tsconfig.json frontend/tsconfig.app.json ./
COPY frontend/src ./src
RUN npm run build

FROM nginx:1.29.1-alpine AS runtime
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/mcp-ai/browser /usr/share/nginx/html
EXPOSE 80
