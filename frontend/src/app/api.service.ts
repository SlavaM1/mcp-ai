import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export interface MCPTool {
  name: string;
  llm_tool_name?: string | null;
  original_name?: string | null;
  tool_name?: string | null;
  server_name?: string | null;
  server_url?: string | null;
  description: string | null;
  input_schema: Record<string, unknown> | null;
}

export interface MCPToolCall {
  id: string;
  sequence?: number;
  name: string;
  server_name?: string | null;
  server_url?: string | null;
  tool_name?: string | null;
  llm_tool_name?: string | null;
  arguments: unknown;
  result?: unknown;
  error?: string | null;
  duration_ms?: number | null;
}

export type MCPAggregateStatus = 'ok' | 'partial' | 'unavailable';

export interface MCPServerStatus {
  server_name: string;
  server_url: string;
  status: 'ok' | 'unavailable';
  tool_count: number;
  error: string | null;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  mcp_data: {
    status?: string;
    server_url?: string | null;
    error?: string | null;
    tools?: MCPTool[];
    available_tools?: MCPTool[];
    tool_calls?: MCPToolCall[];
    servers?: MCPServerStatus[];
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
  status?: MCPAggregateStatus;
  server_url: string | null;
  tool_count: number | null;
  servers?: MCPServerStatus[];
  error: string | null;
}

interface AgentMessageResponse {
  session: ChatSession;
  server_url: string | null;
  servers?: MCPServerStatus[];
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
