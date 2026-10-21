import request from "@/utils/request";

// ---- Types ----

export const TEAM_NAME_PREFIX = "[团队] ";

export interface TeamRosterEntry {
    member_id: string;
    name: string;
    position: string;
    role_note: string;
    joined_at: string;
}

export interface TeamItem {
    id: string;
    name: string; // 已去掉团队前缀的展示名
    raw_name: string;
    description: string;
    identity: string;
    persona: string;
    bible: string;
    leader_id: string;
    roster: TeamRosterEntry[];
    handoff_rules: string;
    sop?: { summary: string; full_text: string };
    config?: Record<string, number>;
    created_at: string | null;
}

export interface TeamCandidate {
    id: string;
    name: string;
    position: string;
    bio: string;
}

export interface TeamGenerateResult {
    identity: string;
    persona: string;
    bible: string;
    matched_functions: { function_id: string; name: string; positioning: string }[];
    suggested_skills: { id: string; name: string; description: string }[];
}

// ---- 团队 CRUD ----

export function getTeams() {
    return request.get("/admin/ai-teams/teams");
}

export function getTeam(teamId: string) {
    return request.get(`/admin/ai-teams/teams/${teamId}`);
}

export function createTeam(data: {
    name: string;
    description?: string;
    members: { member_id: string; role_note?: string }[];
    leader_id?: string;
} & Record<"identity" | "persona" | "bible", string>) {
    return request.post("/admin/ai-teams/teams", data);
}

export function updateTeam(teamId: string, data: Partial<{
    name: string; description: string;
} & Record<"identity" | "persona" | "bible", string>>) {
    return request.put(`/admin/ai-teams/teams/${teamId}`, data);
}

export function updateTeamRoster(teamId: string, data: {
    members: { member_id: string; role_note?: string }[];
    leader_id?: string;
}) {
    return request.put(`/admin/ai-teams/teams/${teamId}/roster`, data);
}

export function changeTeamLeader(teamId: string, leaderId: string) {
    return request.put(`/admin/ai-teams/teams/${teamId}/leader`, { leader_id: leaderId });
}

export function disbandTeam(teamId: string) {
    return request.delete(`/admin/ai-teams/teams/${teamId}`);
}

// ---- 候选 / 模板 / 生成 ----

export function getTeamCandidates() {
    return request.get("/admin/ai-teams/candidates");
}

export function getHandoffTemplate() {
    return request.get("/admin/ai-teams/handoff-template");
}

export function generateTeamPersona(data: {
    name: string;
    description?: string;
    member_posts?: string[];
    regenerate_field?: "identity" | "persona" | "bible";
    current?: Partial<Record<"identity" | "persona" | "bible", string>>;
}) {
    return request.post("/admin/ai-teams/generate", data, { timeout: 180000 });
}


/** 团队沟通 SOP 与编排护栏参数（团队响应队列链路） */
export function updateTeamSopConfig(teamId: string, data: {
    sop_summary?: string;
    sop_full_text?: string;
    config?: Record<string, number>;
}) {
    return request.put(`/admin/ai-teams/teams/${teamId}/sop-config`, data);
}