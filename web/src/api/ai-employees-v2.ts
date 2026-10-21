import request from "@/utils/request";

// ---- Types ----

export interface PersonaParts {
    identity: string;
    persona: string;
    bible: string;
}

export interface V2PostItem extends PersonaParts {
    id: string;
    key: string;
    name: string;
    description: string;
    enabled: boolean;
    skills?: { name: string; description: string }[];
    created_at: string | null;
}

export interface V2MemberItem extends PersonaParts {
    id: string;
    name: string;
    position: string;
    bio: string;
    post: string;
    skill_ids: string[];
    created_at: string | null;
}

export interface GenerateResult extends PersonaParts {
    matched_functions: { function_id: string; name: string; positioning: string }[];
    suggested_skills: { id: string; name: string; description: string }[];
}

// ---- V2 标记（V1 页只读保护用） ----

export function getV2Ids() {
    return request.get("/admin/ai-employees-v2/v2-ids");
}

export function getV2KbStats() {
    return request.get("/admin/ai-employees-v2/kb/stats");
}

// ---- 岗位库 ----

export function getV2Posts() {
    return request.get("/admin/ai-employees-v2/posts");
}

export function createV2Post(data: { name: string; description?: string; key?: string } & PersonaParts) {
    return request.post("/admin/ai-employees-v2/posts", data);
}

export function updateV2Post(postId: string, data: Partial<{ name: string; description: string } & PersonaParts>) {
    return request.put(`/admin/ai-employees-v2/posts/${postId}`, data);
}

export function deleteV2Post(postId: string) {
    return request.delete(`/admin/ai-employees-v2/posts/${postId}`);
}

export function instantiateV2Post(postId: string, data: { name: string; position?: string; bio?: string }) {
    return request.post(`/admin/ai-employees-v2/posts/${postId}/instantiate`, data);
}

// ---- V2 员工 ----

export function getV2Members() {
    return request.get("/admin/ai-employees-v2/members");
}

export function getV2Member(memberId: string) {
    return request.get(`/admin/ai-employees-v2/members/${memberId}`);
}

export function createV2Member(data: {
    name: string; position?: string; bio?: string; post_key?: string;
    skill_ids?: string[];
} & PersonaParts) {
    return request.post("/admin/ai-employees-v2/members", data);
}

export function updateV2Member(memberId: string, data: Partial<{
    name: string; position: string; bio: string; skill_ids: string[];
} & PersonaParts>) {
    return request.put(`/admin/ai-employees-v2/members/${memberId}`, data);
}

// ---- 三件套生成（两步 LLM，超时单独放宽） ----

export function generateV2Persona(data: {
    name: string;
    description?: string;
    regenerate_field?: "identity" | "persona" | "bible";
    current?: Partial<PersonaParts>;
}) {
    return request.post("/admin/ai-employees-v2/generate", data, { timeout: 180000 });
}

// ---- 按员工启用插件（成员绑定） ----

export interface MemberPluginItem {
    name: string;
    display_name: string;
    description: string;
    enabled: boolean;
    visibility: "all" | "members";
    binding_mode: "allow" | "deny" | null;  // null = 未绑定，跟随插件默认可见性
    binding_source: "admin" | "auto" | "";
}

export function getMemberPluginBindings(memberId: string) {
    return request.get(`/admin/plugins/member-bindings/${memberId}`);
}

export function setMemberPluginBinding(
    memberId: string,
    pluginName: string,
    mode: "allow" | "deny" | null,
) {
    return request.put(`/admin/plugins/member-bindings/${memberId}`, {
        bindings: [{ plugin_name: pluginName, mode }],
    });
}
