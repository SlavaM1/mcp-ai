import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export interface MCPTool {
  name: string;
  description: string | null;
  input_schema: Record<string, unknown> | null;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  mcp_data: {
    status?: string;
    server_url?: string;
    error?: string;
    tools?: MCPTool[];
  } | null;
  created_at: string;
}

export interface ChatSessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface ChatSession extends ChatSessionSummary {
  messages: ChatMessage[];
}

export interface MCPStatus {
  connected: boolean;
  server_url: string;
  tool_count: number | null;
  error: string | null;
}

interface ToolListResponse {
  session: ChatSession;
  tools: MCPTool[];
  server_url: string;
  connected: boolean;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);

  listSessions(): Promise<ChatSessionSummary[]> {
    return firstValueFrom(this.http.get<ChatSessionSummary[]>('/api/sessions'));
  }

  getSession(id: string): Promise<ChatSession> {
    return firstValueFrom(this.http.get<ChatSession>(`/api/sessions/${id}`));
  }

  createSession(): Promise<ChatSession> {
    return firstValueFrom(
      this.http.post<ChatSession>('/api/sessions', { title: 'Новый MCP-чат' }),
    );
  }

  listTools(sessionId: string, message: string): Promise<ToolListResponse> {
    return firstValueFrom(
      this.http.post<ToolListResponse>(`/api/sessions/${sessionId}/tools/list`, { message }),
    );
  }

  getMCPStatus(): Promise<MCPStatus> {
    return firstValueFrom(this.http.get<MCPStatus>('/api/mcp/status'));
  }
}
