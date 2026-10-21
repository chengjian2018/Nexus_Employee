<script setup lang="ts">
import { ref, computed, onMounted, watch } from "vue";
import { useRouter } from "vue-router";
import { message } from "@openvort/vort-ui";
import {
    Users, Plus, Trash2, Sparkles, RefreshCw, Crown, BookOpen, Pencil,
    ShieldCheck, MessageCircle,
} from "lucide-vue-next";
import {
    getTeams, createTeam, updateTeam, updateTeamRoster, disbandTeam,
    getTeamCandidates, getHandoffTemplate, generateTeamPersona,
    updateTeamSopConfig, startTeamChat,
    type TeamItem, type TeamRosterEntry, type TeamCandidate,
} from "@/api";

const router = useRouter();

// ---- Types & 常量 ----

interface TeamEditForm {
    id: string;
    name: string;
    description: string;
    identity: string;
    persona: string;
    bible: string;
    leaderId: string;
    roster: TeamRosterEntry[];
    sopSummary: string;
    sopFullText: string;
    maxRuns: number;
    maxRework: number;
    runTimeout: number;
    maxRoundsMember: number;
    maxRoundsLeader: number;
}

// 编排护栏默认值（与后端 TEAM_CONFIG_DEFAULTS 对齐）
const CONFIG_META: { key: keyof TeamEditForm; label: string; hint: string }[] = [
    { key: "maxRuns", label: "单消息 run 上限", hint: "一条用户消息触发的成员执行总次数" },
    { key: "maxRework", label: "返工次数上限", hint: "同一任务允许的返工次数" },
    { key: "runTimeout", label: "单次超时（分钟）", hint: "一次成员执行的超时时间" },
    { key: "maxRoundsMember", label: "成员轮次上限", hint: "成员 run 的工具循环轮数" },
    { key: "maxRoundsLeader", label: "负责人轮次上限", hint: "负责人 run 的工具循环轮数" },
];

const FIELD_META: { key: "identity" | "persona" | "bible"; title: string; hint: string }[] = [
    { key: "identity", title: "IDENTITY · 职责与边界", hint: "团队的核心定位 / 职责范围 / 边界 / 降级策略" },
    { key: "persona", title: "PERSONA · 工作风格", hint: "团队整体的工作风格 / 沟通方式 / 反模式" },
    { key: "bible", title: "BIBLE · 工作手册", hint: "接单→拆解→指派→汇总交付的通用流程" },
];

// ---- 列表 ----

const teams = ref<TeamItem[]>([]);
const candidates = ref<TeamCandidate[]>([]);
const loading = ref(false);

const candidatesById = computed(() => {
    const map: Record<string, TeamCandidate> = {};
    for (const c of candidates.value) map[c.id] = c;
    return map;
});

async function loadTeams() {
    try {
        const res: any = await getTeams();
        teams.value = res?.teams || [];
    } catch { message.error("加载团队列表失败"); }
}

async function loadCandidates() {
    try {
        const res: any = await getTeamCandidates();
        candidates.value = res?.candidates || [];
    } catch { /* ignore */ }
}

function leaderName(team: TeamItem): string {
    return team.roster.find(e => e.member_id === team.leader_id)?.name || "未设置";
}

// ---- 新建团队抽屉 ----

const createOpen = ref(false);
const createSaving = ref(false);
const genLoading = ref(false);
const regenField = ref("");
const createForm = ref({
    name: "", description: "",
    identity: "", persona: "", bible: "",
    memberIds: [] as string[], leaderId: "",
});
const createGenDone = ref(false);

// 建团草稿：抽屉误关/切页再回来不丢已填内容与已生成的三件套；创建成功才清空
const CREATE_DRAFT_KEY = "ai-team-create-draft";

function saveCreateDraft() {
    try {
        sessionStorage.setItem(CREATE_DRAFT_KEY, JSON.stringify({
            form: createForm.value, genDone: createGenDone.value,
        }));
    } catch { /* ignore quota errors */ }
}

function clearCreateDraft() {
    try { sessionStorage.removeItem(CREATE_DRAFT_KEY); } catch { /* ignore */ }
}

watch(createForm, saveCreateDraft, { deep: true });

function openCreate() {
    let restored = false;
    try {
        const saved = sessionStorage.getItem(CREATE_DRAFT_KEY);
        if (saved) {
            const draft = JSON.parse(saved);
            if (draft?.form && (draft.form.name || draft.form.description
                || draft.form.memberIds?.length || draft.form.identity)) {
                createForm.value = {
                    name: draft.form.name || "",
                    description: draft.form.description || "",
                    identity: draft.form.identity || "",
                    persona: draft.form.persona || "",
                    bible: draft.form.bible || "",
                    memberIds: Array.isArray(draft.form.memberIds) ? draft.form.memberIds : [],
                    leaderId: draft.form.leaderId || "",
                };
                createGenDone.value = !!draft.genDone;
                restored = true;
            }
        }
    } catch { /* 坏数据按无草稿处理 */ }
    if (!restored) {
        createForm.value = { name: "", description: "", identity: "", persona: "", bible: "", memberIds: [], leaderId: "" };
        createGenDone.value = false;
    }
    createOpen.value = true;
}

function discardCreateDraft() {
    clearCreateDraft();
    createForm.value = { name: "", description: "", identity: "", persona: "", bible: "", memberIds: [], leaderId: "" };
    createGenDone.value = false;
}

function onCreateMembersChange(ids: any) {
    const list = (ids || []) as string[];
    createForm.value.memberIds = list;
    // 默认第一个被拉进来的成员是负责人；已选负责人被移出时回落到第一位
    if (!list.includes(createForm.value.leaderId)) {
        createForm.value.leaderId = list[0] || "";
    }
}

async function handleCreateGenerate() {
    if (!createForm.value.name.trim()) {
        message.warning("请先填写团队名称");
        return;
    }
    genLoading.value = true;
    try {
        const memberPosts = createForm.value.memberIds
            .map(id => candidatesById.value[id]?.position || candidatesById.value[id]?.name || "")
            .filter(Boolean);
        const res: any = await generateTeamPersona({
            name: createForm.value.name.trim(),
            description: createForm.value.description.trim(),
            member_posts: memberPosts,
        });
        createForm.value.identity = res?.identity || "";
        createForm.value.persona = res?.persona || "";
        createForm.value.bible = res?.bible || "";
        createGenDone.value = true;
    } catch (e: any) {
        message.error(e?.response?.data?.detail || e?.message || "生成失败，请重试");
    } finally {
        genLoading.value = false;
    }
}

async function handleCreateRegenerate(field: "identity" | "persona" | "bible") {
    regenField.value = field;
    try {
        const res: any = await generateTeamPersona({
            name: createForm.value.name.trim(),
            description: createForm.value.description.trim(),
            regenerate_field: field,
            current: {
                identity: createForm.value.identity,
                persona: createForm.value.persona,
                bible: createForm.value.bible,
            },
        });
        (createForm.value as any)[field] = res?.[field] || "";
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "重新生成失败");
    } finally {
        regenField.value = "";
    }
}

async function handleCreateTeam() {
    const f = createForm.value;
    if (!f.name.trim()) { message.warning("请填写团队名称"); return; }
    if (f.memberIds.length < 2) { message.warning("团队至少需要 2 名成员（含负责人）"); return; }
    if (!f.identity.trim() || !f.persona.trim() || !f.bible.trim()) {
        message.warning("请先生成或手动填写团队三件套");
        return;
    }
    createSaving.value = true;
    try {
        await createTeam({
            name: f.name.trim(),
            description: f.description.trim(),
            identity: f.identity, persona: f.persona, bible: f.bible,
            members: f.memberIds.map(id => ({ member_id: id })),
            leader_id: f.leaderId || f.memberIds[0],
        });
        message.success("团队已创建");
        clearCreateDraft();
        createOpen.value = false;
        await loadTeams();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "创建失败");
    } finally {
        createSaving.value = false;
    }
}

// ---- 编辑团队抽屉 ----

const editOpen = ref(false);
const editSaving = ref(false);
const editShowPersona = ref(false);
const editAddId = ref("");
const editing = ref<TeamEditForm | null>(null);

async function openTeamChat(team: TeamItem) {
    try {
        const res: any = await startTeamChat(team.id);
        router.push({
            name: "chat",
            query: {
                type: "team",
                contact: team.id,
                ...(res?.session_id ? { session: res.session_id } : {}),
            },
        });
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "进入聊天失败");
    }
}

function openEdit(team: TeamItem) {
    const cfg = team.config || {};
    editing.value = {
        id: team.id,
        name: team.name,
        description: team.description,
        identity: team.identity, persona: team.persona, bible: team.bible,
        leaderId: team.leader_id,
        roster: team.roster.map(e => ({ ...e })),
        sopSummary: team.sop?.summary || "",
        sopFullText: team.sop?.full_text || "",
        maxRuns: cfg.max_runs ?? 15,
        maxRework: cfg.max_rework ?? 3,
        runTimeout: cfg.run_timeout ?? 15,
        maxRoundsMember: cfg.max_rounds_member ?? 25,
        maxRoundsLeader: cfg.max_rounds_leader ?? 15,
    };
    editShowPersona.value = false;
    editAddId.value = "";
    editOpen.value = true;
}

const editAddableCandidates = computed(() => {
    const inTeam = new Set((editing.value?.roster || []).map(e => e.member_id));
    return candidates.value.filter(c => !inTeam.has(c.id));
});

function editRemoveEntry(entry: TeamRosterEntry) {
    if (!editing.value) return;
    if (entry.member_id === editing.value.leaderId) {
        message.warning("负责人不能被移除，请先更换负责人");
        return;
    }
    if (editing.value.roster.length <= 2) {
        message.warning("团队至少需要 2 名成员；如需解散请使用解散操作");
        return;
    }
    editing.value.roster = editing.value.roster.filter(e => e.member_id !== entry.member_id);
}

function editAddMember() {
    if (!editing.value || !editAddId.value) return;
    const c = candidatesById.value[editAddId.value];
    if (!c) return;
    if (editing.value.roster.some(e => e.member_id === c.id)) return;
    editing.value.roster.push({
        member_id: c.id, name: c.name, position: c.position || "",
        role_note: "", joined_at: "",
    });
    editAddId.value = "";
}

async function handleSaveEdit() {
    const form = editing.value;
    if (!form?.name.trim()) { message.warning("团队名称不能为空"); return; }
    if (form.roster.length < 2) { message.warning("团队至少需要 2 名成员"); return; }
    if (!form.roster.some(e => e.member_id === form.leaderId)) {
        message.warning("负责人必须在成员列表中");
        return;
    }
    editSaving.value = true;
    try {
        await updateTeam(form.id, {
            name: form.name.trim(), description: form.description.trim(),
            identity: form.identity, persona: form.persona, bible: form.bible,
        });
        await updateTeamRoster(form.id, {
            members: form.roster.map(e => ({ member_id: e.member_id, role_note: e.role_note })),
            leader_id: form.leaderId,
        });
        await updateTeamSopConfig(form.id, {
            sop_summary: form.sopSummary.trim(),
            sop_full_text: form.sopFullText.trim(),
            config: {
                max_runs: Number(form.maxRuns) || 15,
                max_rework: Number(form.maxRework) || 3,
                run_timeout: Number(form.runTimeout) || 15,
                max_rounds_member: Number(form.maxRoundsMember) || 25,
                max_rounds_leader: Number(form.maxRoundsLeader) || 15,
            },
        });
        message.success("团队已更新");
        editOpen.value = false;
        await loadTeams();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "保存失败");
    } finally {
        editSaving.value = false;
    }
}

async function handleDisband(team: TeamItem) {
    try {
        await disbandTeam(team.id);
        message.success(`团队「${team.name}」已解散，成员员工已保留`);
        await loadTeams();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "解散失败");
    }
}

// ---- 移交规则模板（只读） ----

const templateOpen = ref(false);
const templateText = ref("");

async function openTemplate() {
    if (!templateText.value) {
        try {
            const res: any = await getHandoffTemplate();
            templateText.value = res?.template || "";
        } catch { message.error("加载移交规则模板失败"); return; }
    }
    templateOpen.value = true;
}

// ---- Init ----

onMounted(async () => {
    loading.value = true;
    await Promise.all([loadTeams(), loadCandidates()]);
    loading.value = false;
});
</script>

<template>
    <div class="space-y-5">
        <!-- Header -->
        <div class="flex items-center justify-between">
            <div>
                <h2 class="text-lg font-medium text-gray-800 flex items-center gap-2">
                    AI 团队
                    <VortTag color="green" :bordered="false" size="small">团队即员工</VortTag>
                </h2>
                <p class="text-sm text-gray-500 mt-1">
                    一个团队 = 一名多能力的 AI 员工：由负责人承接需求，成员按花名册分工协作，对外统一交付
                </p>
            </div>
            <div class="flex items-center gap-2">
                <VortButton size="small" @click="openTemplate">
                    <BookOpen :size="13" class="mr-1" /> 移交规则
                </VortButton>
                <VortButton variant="primary" @click="openCreate">
                    <Plus :size="14" class="mr-1" /> 新建团队
                </VortButton>
            </div>
        </div>

        <!-- Team cards -->
        <VortSpin :spinning="loading">
            <div v-if="teams.length" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                <div
                    v-for="team in teams" :key="team.id"
                    class="bg-white rounded-xl border border-gray-100 p-4 hover:shadow-md hover:border-gray-200 transition-all"
                >
                    <div class="flex items-start gap-3">
                        <div class="w-10 h-10 rounded-lg bg-emerald-50 flex items-center justify-center flex-shrink-0">
                            <Users :size="20" class="text-emerald-600" />
                        </div>
                        <div class="min-w-0 flex-1">
                            <div class="font-medium text-gray-800 truncate" :title="team.name">{{ team.name }}</div>
                            <div class="text-xs text-gray-400 mt-0.5 flex items-center gap-1">
                                <Crown :size="12" class="text-amber-500" />
                                {{ leaderName(team) }} · {{ team.roster.length }} 人
                            </div>
                        </div>
                    </div>
                    <div class="text-xs text-gray-500 mt-3 line-clamp-2 min-h-[32px]">
                        {{ team.description || '暂无描述' }}
                    </div>
                    <div class="flex flex-wrap gap-1 mt-2 min-h-[22px]">
                        <VortTag
                            v-for="entry in team.roster.slice(0, 3)" :key="entry.member_id"
                            size="small" :color="entry.member_id === team.leader_id ? 'amber' : 'default'"
                            :bordered="false" :title="entry.role_note || entry.position"
                        >{{ entry.name }}</VortTag>
                        <VortTag v-if="team.roster.length > 3" size="small" color="default" :bordered="false">
                            +{{ team.roster.length - 3 }}
                        </VortTag>
                    </div>
                    <div class="flex items-center justify-end gap-1 mt-3 pt-3 border-t border-gray-50">
                        <VortButton size="small" @click="openTeamChat(team)">
                            <MessageCircle :size="12" class="mr-1" /> 进入聊天
                        </VortButton>
                        <VortButton size="small" @click="openEdit(team)">
                            <Pencil :size="12" class="mr-1" /> 管理
                        </VortButton>
                        <VortPopconfirm
                            title="确认解散该团队？团队行将被删除，成员员工全部保留。"
                            @confirm="handleDisband(team)"
                        >
                            <VortButton size="small" danger>
                                <Trash2 :size="12" />
                            </VortButton>
                        </VortPopconfirm>
                    </div>
                </div>
            </div>
            <div v-else-if="!loading" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
                <Users :size="48" class="mx-auto text-gray-300 mb-4" />
                <div class="text-gray-500 mb-2">暂无团队</div>
                <div class="text-xs text-gray-400 mb-4">一个团队至少 2 名 AI 员工，第一个成员默认为团队负责人</div>
                <VortButton variant="primary" size="small" @click="openCreate">
                    <Plus :size="14" class="mr-1" /> 创建第一个团队
                </VortButton>
            </div>
        </VortSpin>

        <!-- Create drawer -->
        <VortDrawer v-model:open="createOpen" title="新建团队" :width="760">
            <div class="space-y-4">
                <VortForm label-width="90px">
                    <VortFormItem label="团队名称" required>
                        <div class="flex items-center gap-2">
                            <VortTag color="green" :bordered="false">[团队]</VortTag>
                            <VortInput v-model="createForm.name" placeholder="如：产品研发组、内容运营组" style="flex:1" />
                        </div>
                    </VortFormItem>
                    <VortFormItem label="团队描述">
                        <VortTextarea
                            v-model="createForm.description" :rows="3"
                            placeholder="这个团队对用户提供什么能力、由哪些角色构成、边界在哪。描述越具体，三件套生成质量越高。"
                        />
                    </VortFormItem>
                    <VortFormItem label="团队成员" required>
                        <VortSelect
                            :model-value="createForm.memberIds"
                            mode="multiple" placeholder="选择至少 2 名 AI 员工（不含团队）"
                            style="width: 100%" :max-tag-count="6"
                            @update:model-value="onCreateMembersChange"
                        >
                            <VortSelectOption v-for="c in candidates" :key="c.id" :value="c.id">
                                {{ c.name }}{{ c.position ? ` · ${c.position}` : "" }}
                            </VortSelectOption>
                        </VortSelect>
                    </VortFormItem>
                    <VortFormItem label="负责人">
                        <VortSelect
                            v-model="createForm.leaderId"
                            placeholder="默认第一个加入的成员"
                            style="width: 100%" allow-clear
                        >
                            <VortSelectOption
                                v-for="id in createForm.memberIds" :key="id" :value="id"
                            >{{ candidatesById[id]?.name || id }}</VortSelectOption>
                        </VortSelect>
                        <div class="text-xs text-gray-400 mt-1">负责人承接用户需求、按花名册分工并汇总交付</div>
                    </VortFormItem>
                </VortForm>

                <div class="flex items-center justify-between">
                    <div class="text-sm font-semibold text-gray-700 flex items-center gap-1">
                        <ShieldCheck :size="14" class="text-emerald-600" /> 团队三件套
                    </div>
                    <VortButton size="small" :loading="genLoading" @click="handleCreateGenerate">
                        <Sparkles :size="12" class="mr-1" /> {{ createGenDone ? "全部重新生成" : "生成三件套" }}
                    </VortButton>
                </div>
                <div v-for="meta in FIELD_META" :key="meta.key" class="rounded-lg border border-gray-100">
                    <div class="flex items-center justify-between px-3 py-2 border-b border-gray-100 bg-gray-50/60 rounded-t-lg">
                        <div>
                            <span class="text-sm font-semibold text-gray-700">{{ meta.title }}</span>
                            <span class="text-xs text-gray-400 ml-2">{{ meta.hint }}</span>
                        </div>
                        <VortButton
                            v-if="createGenDone" size="small"
                            :loading="regenField === meta.key"
                            :disabled="genLoading && regenField !== meta.key"
                            @click="handleCreateRegenerate(meta.key)"
                        >
                            <RefreshCw :size="12" class="mr-1" /> 重生成
                        </VortButton>
                    </div>
                    <VortTextarea v-model="(createForm as any)[meta.key]" :rows="7" />
                </div>

                <div class="flex items-center justify-end gap-2">
                    <VortPopconfirm title="清空当前草稿？已填内容与生成的三件套将丢失。" @confirm="discardCreateDraft">
                        <VortButton size="small">清空草稿</VortButton>
                    </VortPopconfirm>
                    <VortButton variant="primary" :loading="createSaving" @click="handleCreateTeam">创建团队</VortButton>
                </div>
            </div>
        </VortDrawer>

        <!-- Edit drawer -->
        <VortDrawer v-model:open="editOpen" :title="editing ? `管理团队 · ${editing.name}` : ''" :width="760">
            <div v-if="editing" class="space-y-4">
                <VortForm label-width="90px">
                    <VortFormItem label="团队名称" required>
                        <div class="flex items-center gap-2">
                            <VortTag color="green" :bordered="false">[团队]</VortTag>
                            <VortInput v-model="editing.name" style="flex:1" />
                        </div>
                    </VortFormItem>
                    <VortFormItem label="团队描述">
                        <VortTextarea v-model="editing.description" :rows="2" />
                    </VortFormItem>
                    <VortFormItem label="负责人">
                        <VortSelect v-model="editing.leaderId" style="width: 100%">
                            <VortSelectOption
                                v-for="entry in editing.roster" :key="entry.member_id" :value="entry.member_id"
                            >{{ entry.name }}</VortSelectOption>
                        </VortSelect>
                    </VortFormItem>
                </VortForm>

                <!-- 花名册 -->
                <div>
                    <div class="text-sm font-semibold text-gray-700 mb-2 flex items-center gap-1">
                        <Users :size="14" /> 花名册（{{ editing.roster.length }} 人）
                    </div>
                    <div class="rounded-lg border border-gray-100 divide-y divide-gray-50">
                        <div v-for="entry in editing.roster" :key="entry.member_id" class="flex items-center gap-3 px-3 py-2">
                            <Crown
                                :size="14"
                                :class="entry.member_id === editing.leaderId ? 'text-amber-500' : 'text-transparent'"
                            />
                            <div class="min-w-0 w-32">
                                <div class="text-sm text-gray-800 truncate">{{ entry.name }}</div>
                                <div class="text-xs text-gray-400 truncate">{{ entry.position || '—' }}</div>
                            </div>
                            <VortInput
                                v-model="entry.role_note" size="small" placeholder="分工注记（可选）" style="flex:1"
                            />
                            <VortButton size="small" danger @click="editRemoveEntry(entry)">
                                <Trash2 :size="12" />
                            </VortButton>
                        </div>
                    </div>
                    <div class="flex items-center gap-2 mt-2">
                        <VortSelect
                            v-model="editAddId"
                            placeholder="选择要拉入的 AI 员工"
                            style="flex:1" allow-clear
                        >
                            <VortSelectOption v-for="c in editAddableCandidates" :key="c.id" :value="c.id">
                                {{ c.name }}{{ c.position ? ` · ${c.position}` : "" }}
                            </VortSelectOption>
                        </VortSelect>
                        <VortButton size="small" :disabled="!editAddId" @click="editAddMember">
                            <Plus :size="12" class="mr-1" /> 拉入
                        </VortButton>
                    </div>
                </div>

                <!-- 三件套折叠 -->
                <div>
                    <VortButton size="small" @click="editShowPersona = !editShowPersona">
                        {{ editShowPersona ? "收起三件套" : "展开三件套" }}
                    </VortButton>
                    <template v-if="editShowPersona">
                        <div v-for="meta in FIELD_META" :key="meta.key" class="rounded-lg border border-gray-100 mt-2">
                            <div class="px-3 py-2 border-b border-gray-100 bg-gray-50/60 rounded-t-lg">
                                <span class="text-sm font-semibold text-gray-700">{{ meta.title }}</span>
                            </div>
                            <VortTextarea v-model="(editing as any)[meta.key]" :rows="7" />
                        </div>
                    </template>
                </div>

                <!-- 团队沟通 SOP（编排链路：摘要常驻人设，全文成员按需读） -->
                <div class="rounded-lg border border-gray-100">
                    <div class="px-3 py-2 border-b border-gray-100 bg-gray-50/60 rounded-t-lg flex items-center gap-1.5">
                        <BookOpen :size="14" class="text-emerald-600" />
                        <span class="text-sm font-semibold text-gray-700">团队沟通 SOP</span>
                        <span class="text-xs text-gray-400">（摘要常驻，全文成员用 team_read_sop 按需读）</span>
                    </div>
                    <div class="p-3 space-y-2">
                        <div>
                            <div class="text-xs text-gray-500 mb-1">摘要（约 200 字，常驻 system prompt）</div>
                            <VortTextarea v-model="editing.sopSummary" :rows="4"
                                placeholder="例：需求先由负责人评估并公屏首答；产出统一放共享电脑 public 目录；阶段完成必须 team_report 交接…" />
                        </div>
                        <div>
                            <div class="text-xs text-gray-500 mb-1">全文（成员需要核对流程细节时读取）</div>
                            <VortTextarea v-model="editing.sopFullText" :rows="8"
                                placeholder="完整的团队沟通规范：汇报口径、评审流程、异常处理、返工规则…" />
                        </div>
                    </div>
                </div>

                <!-- 编排护栏 -->
                <div class="rounded-lg border border-gray-100">
                    <div class="px-3 py-2 border-b border-gray-100 bg-gray-50/60 rounded-t-lg flex items-center gap-1.5">
                        <ShieldCheck :size="14" class="text-emerald-600" />
                        <span class="text-sm font-semibold text-gray-700">编排护栏</span>
                        <span class="text-xs text-gray-400">（超限自动暂停等用户，新消息恢复）</span>
                    </div>
                    <div class="p-3 grid grid-cols-2 gap-2">
                        <div v-for="meta in CONFIG_META" :key="meta.key">
                            <div class="text-xs text-gray-500 mb-1" :title="meta.hint">{{ meta.label }}</div>
                            <VortInputNumber v-model="(editing as any)[meta.key]" :min="1" class="w-full" />
                        </div>
                    </div>
                </div>

                <div class="flex items-center justify-between pt-2 border-t border-gray-100">
                    <VortPopconfirm
                        title="确认解散该团队？团队行将被删除，成员员工全部保留。"
                        @confirm="editing && handleDisband({ id: editing.id, name: editing.name } as any); editOpen = false"
                    >
                        <VortButton danger size="small">
                            <Trash2 :size="12" class="mr-1" /> 解散团队
                        </VortButton>
                    </VortPopconfirm>
                    <VortButton variant="primary" :loading="editSaving" @click="handleSaveEdit">保存</VortButton>
                </div>
            </div>
        </VortDrawer>

        <!-- Handoff template drawer (read-only) -->
        <VortDrawer v-model:open="templateOpen" title="团队协作与任务移交规则（全局模板 · 只读）" :width="640">
            <pre class="text-sm text-gray-700 whitespace-pre-wrap font-sans leading-relaxed">{{ templateText || "模板为空" }}</pre>
            <div class="text-xs text-gray-400 mt-4">
                模板在建团时快照进团队人设；模板更新只影响之后新建的团队，已建团队不自动回填。
            </div>
        </VortDrawer>
    </div>
</template>
