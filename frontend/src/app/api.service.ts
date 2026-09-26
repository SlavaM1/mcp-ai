import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export interface MCPTool {
  name: string;
  description: string | null;
  input_schema: Record<string, unknown> | null;
}

export interface MCPToolCall {
  id: string;
  sequence?: number;
  name: string;
  arguments: unknown;
  result?: unknown;
  error?: string | null;
  duration_ms?: number | null;
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
    tool_calls?: MCPToolCall[];
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

interface AgentMessageResponse {
  session: ChatSession;
  server_url: string;
  tool_calls: MCPToolCall[];
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

  sendMessage(sessionId: string, message: string): Promise<AgentMessageResponse> {
    return firstValueFrom(
      this.http.post<AgentMessageResponse>(`/api/sessions/${sessionId}/messages`, { message }),
    );
  }

  getMCPStatus(): Promise<MCPStatus> {
    return firstValueFrom(this.http.get<MCPStatus>('/api/mcp/status'));
  }
}
