import { DatePipe, JsonPipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Component, ElementRef, inject, OnInit, signal, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';

import {
  ApiService,
  ChatSession,
  ChatSessionSummary,
  MCPStatus,
  MCPTool,
} from './api.service';

@Component({
  selector: 'app-root',
  imports: [DatePipe, FormsModule, JsonPipe],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App implements OnInit {
  @ViewChild('composerInput') private composerInput?: ElementRef<HTMLTextAreaElement>;

  private readonly api = inject(ApiService);
  private loadSequence = 0;

  readonly sessions = signal<ChatSessionSummary[]>([]);
  readonly activeSession = signal<ChatSession | null>(null);
  readonly status = signal<MCPStatus | null>(null);
  readonly loadingSessions = signal(true);
  readonly statusLoading = signal(true);
  readonly pending = signal(false);
  readonly error = signal<string | null>(null);
  readonly sidebarCollapsed = signal(false);

  draft = 'Получить список MCP-инструментов';

  ngOnInit(): void {
    void Promise.all([this.loadSessions(), this.refreshStatus()]);
  }

  async loadSessions(preferredId?: string): Promise<void> {
    const sequence = ++this.loadSequence;
    this.loadingSessions.set(true);
    try {
      const sessions = await this.api.listSessions();
      if (sequence !== this.loadSequence) return;
      this.sessions.set(sessions);
      const selectedId = preferredId ?? this.activeSession()?.id ?? sessions[0]?.id;
      if (selectedId) {
        await this.selectSession(selectedId, sequence);
      } else {
        this.activeSession.set(null);
      }
    } catch (error: unknown) {
      if (sequence === this.loadSequence) this.error.set(this.errorText(error));
    } finally {
      if (sequence === this.loadSequence) this.loadingSessions.set(false);
    }
  }

  async selectSession(id: string, parentSequence?: number): Promise<void> {
    const sequence = parentSequence ?? ++this.loadSequence;
    this.error.set(null);
    try {
      const session = await this.api.getSession(id);
      if (sequence === this.loadSequence) this.activeSession.set(session);
    } catch (error: unknown) {
      if (sequence === this.loadSequence) this.error.set(this.errorText(error));
    }
  }

  async createSession(): Promise<void> {
    if (this.pending()) return;
    this.pending.set(true);
    this.error.set(null);
    try {
      const created = await this.api.createSession();
      this.activeSession.set(created);
      await this.loadSessions(created.id);
      queueMicrotask(() => this.composerInput?.nativeElement.focus());
    } catch (error: unknown) {
      this.error.set(this.errorText(error));
    } finally {
      this.pending.set(false);
    }
  }

  async requestTools(): Promise<void> {
    const message = this.draft.trim();
    if (!message || this.pending()) return;
    this.pending.set(true);
    this.error.set(null);
    let session = this.activeSession();
    try {
      if (!session) {
        session = await this.api.createSession();
        this.activeSession.set(session);
      }
      const result = await this.api.listTools(session.id, message);
      this.activeSession.set(result.session);
      this.status.set({
        connected: true,
        server_url: result.server_url,
        tool_count: result.tools.length,
        error: null,
      });
      await this.reloadSessionList(session.id);
    } catch (error: unknown) {
      this.error.set(this.errorText(error));
      if (session) {
        try {
          this.activeSession.set(await this.api.getSession(session.id));
          await this.reloadSessionList(session.id);
        } catch {
          // Keep the original actionable error visible.
        }
      }
      await this.refreshStatus();
    } finally {
      this.pending.set(false);
      queueMicrotask(() => this.composerInput?.nativeElement.focus());
    }
  }

  async refreshStatus(): Promise<void> {
    this.statusLoading.set(true);
    try {
      this.status.set(await this.api.getMCPStatus());
    } catch (error: unknown) {
      this.status.set({
        connected: false,
        server_url: 'MCP_SERVER_URL',
        tool_count: null,
        error: this.errorText(error),
      });
    } finally {
      this.statusLoading.set(false);
    }
  }

  handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.requestTools();
    }
  }

  toolsFor(message: { mcp_data: { tools?: MCPTool[] } | null }): MCPTool[] {
    return message.mcp_data?.tools ?? [];
  }

  toggleSidebar(): void {
    this.sidebarCollapsed.update((value) => !value);
  }

  dismissError(): void {
    this.error.set(null);
  }

  private async reloadSessionList(activeId: string): Promise<void> {
    const summaries = await this.api.listSessions();
    this.sessions.set(summaries);
    const summary = summaries.find(({ id }) => id === activeId);
    const current = this.activeSession();
    if (summary && current) this.activeSession.set({ ...current, ...summary });
  }

  private errorText(error: unknown): string {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail;
      if (typeof detail === 'string') return detail;
      if (error.status === 0) return 'Backend недоступен. Проверьте состояние контейнеров.';
    }
    return error instanceof Error ? error.message : 'Не удалось выполнить запрос.';
  }
}
