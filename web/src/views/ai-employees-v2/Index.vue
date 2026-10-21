<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { message } from "@openvort/vort-ui";
import {
    Sparkles, Bot, Pencil, Trash2, UserPlus, MessageSquare, Plus,
    RefreshCw, ArrowLeft, Layers, BookOpen, BadgeCheck,
} from "lucide-vue-next";
import {
    getV2Posts, getV2Members, getV2KbStats, getV2Member,
    createV2Post, updateV2Post, deleteV2Post, instantiateV2Post,
    createV2Member, updateV2Member, generateV2Persona,
    getMemberPluginBindings, setMemberPluginBinding,
    type V2PostItem, type V2MemberItem, type GenerateResult,
    type MemberPluginItem,
} from "@/api";
import { getSkills, deleteMember, startMemberChat } from "@/api";

const router = useRouter();

// ---- Types ----

interface SkillOption {
    id: string;
    name: string;
    description: string;
}

interface PostEditForm {
    id?: string;
    name: string;
    description: string;
    identity: string;
    persona: string;
    bible: string;
    skills?: { name: string; description: string }[];
}

interface MemberEditForm {
    id?: string;
    name: string;
    position: string;
    bio: string;
    post_key?: string;
    identity: string;
    persona: string;
    bible: string;
    skill_ids: string[];
}

const FIELD_META: { key: "identity" | "persona" | "bible"; title: string; hint: string }[] = [
    { key: "identity", title: "IDENTITY · 职责与边界", hint: "核心定位 / 核心职责 3~6 条 / 职责边界 / 降级策略" },
    { key: "persona", title: "PERSONA · 工作风格", hint: "工作风格 / 沟通方式 / 处理不确定性 / 反模式" },
    { key: "bible", title: "BIBLE · 工作手册", hint: "通用工作流程 4~6 步 / 需要用户确认的动作 / 交付标准" },
];

// ---- State: tabs & lists ----

const activeTab = ref<"posts" | "members">("posts");
const posts = ref<V2PostItem[]>([]);
const members = ref<V2MemberItem[]>([]);
const loading = ref(false);
const kbStats = ref({ functions: 0, skill_refs: 0, mappings: 0 });

async function loadPosts() {
    try {
        const res: any = await getV2Posts();
        posts.value = res?.posts || [];
    } catch { message.error("加载岗位库失败"); }
}

async function loadMembers() {
    try {
        const res: any = await getV2Members();
        members.value = res?.members || [];
    } catch { message.error("加载 V2 员工失败"); }
}

async function loadKbStats() {
    try {
        const res: any = await getV2KbStats();
        kbStats.value = res || kbStats.value;
    } catch { /* ignore */ }
}

// ---- State: generate drawer ----

const genOpen = ref(false);
const genLoading = ref(false);
const genForm = ref({ name: "", description: "" });
const genResult = ref<GenerateResult | null>(null);
const genSelectedSkills = ref<string[]>([]);
const regenField = ref("");  // 正在单独重生成的字段

function openGenerate() {
    genResult.value = null;
    genSelectedSkills.value = [];
    genForm.value = { name: "", description: "" };
    genOpen.value = true;
}

async function handleGenerate() {
    if (!genForm.value.name.trim()) {
        message.warning("请填写岗位/员工名称");
        return;
    }
    genLoading.value = true;
    try {
        const res: any = await generateV2Persona({
            name: genForm.value.name.trim(),
            description: genForm.value.description.trim(),
        });
        genResult.value = res;
        genSelectedSkills.value = (res?.suggested_skills || []).map((s: SkillOption) => s.id);
    } catch (e: any) {
        message.error(e?.response?.data?.detail || e?.message || "生成失败，请重试");
    } finally {
        genLoading.value = false;
    }
}

async function handleRegenerate(field: "identity" | "persona" | "bible") {
    if (!genResult.value) return;
    regenField.value = field;
    try {
        const res: any = await generateV2Persona({
            name: genForm.value.name.trim(),
            description: genForm.value.description.trim(),
            regenerate_field: field,
            current: {
                identity: genResult.value.identity,
                persona: genResult.value.persona,
                bible: genResult.value.bible,
            },
        });
        (genResult.value as any)[field] = res?.[field] || "";
    } catch (e: any) {
        message.error(e?.response?.data?.detail || e?.message || "重新生成失败");
    } finally {
        regenField.value = "";
    }
}

async function handleSaveAsPost() {
    const r = genResult.value;
    if (!r || !genForm.value.name.trim()) return;
    try {
        await createV2Post({
            name: genForm.value.name.trim(),
            description: genForm.value.description.trim(),
            identity: r.identity, persona: r.persona, bible: r.bible,
        });
        message.success("已保存为 V2 岗位");
        genOpen.value = false;
        await loadPosts();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "保存失败");
    }
}

async function handleSaveAsMember() {
    const r = genResult.value;
    if (!r || !genForm.value.name.trim()) return;
    try {
        await createV2Member({
            name: genForm.value.name.trim(),
            position: genForm.value.name.trim(),
            bio: genForm.value.description.trim(),
            identity: r.identity, persona: r.persona, bible: r.bible,
            skill_ids: genSelectedSkills.value,
        });
        message.success("已创建 V2 员工");
        genOpen.value = false;
        activeTab.value = "members";
        await loadMembers();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "保存失败");
    }
}

// ---- State: post edit drawer ----

const postEditOpen = ref(false);
const postEditing = ref<PostEditForm | null>(null);
const postSaving = ref(false);

function openEditPost(post: V2PostItem) {
    postEditing.value = {
        id: post.id, name: post.name, description: post.description,
        identity: post.identity, persona: post.persona, bible: post.bible,
        skills: post.skills || [],
    };
    postEditOpen.value = true;
}

function openCreatePost() {
    postEditing.value = { name: "", description: "", identity: "", persona: "", bible: "" };
    postEditOpen.value = true;
}

async function handleSavePost() {
    const form = postEditing.value;
    if (!form?.name.trim()) {
        message.warning("请填写岗位名称");
        return;
    }
    if (!form.identity.trim() || !form.persona.trim() || !form.bible.trim()) {
        message.warning("IDENTITY / PERSONA / BIBLE 三份文件都不能为空");
        return;
    }
    postSaving.value = true;
    try {
        if (form.id) {
            await updateV2Post(form.id, {
                name: form.name, description: form.description,
                identity: form.identity, persona: form.persona, bible: form.bible,
            });
            message.success("岗位已更新");
        } else {
            await createV2Post({
                name: form.name.trim(), description: form.description.trim(),
                identity: form.identity, persona: form.persona, bible: form.bible,
            });
            message.success("岗位已创建");
        }
        postEditOpen.value = false;
        await loadPosts();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "保存失败");
    } finally {
        postSaving.value = false;
    }
}

async function handleDeletePost(post: V2PostItem) {
    try {
        await deleteV2Post(post.id);
        message.success("岗位已禁用");
        await loadPosts();
    } catch { message.error("操作失败"); }
}

// ---- State: instantiate ----

const instOpen = ref(false);
const instTarget = ref<V2PostItem | null>(null);
const instForm = ref({ name: "", position: "", bio: "" });
const instSaving = ref(false);

function openInstantiate(post: V2PostItem) {
    instTarget.value = post;
    instForm.value = { name: post.name, position: post.name, bio: post.description };
    instOpen.value = true;
}

async function handleInstantiate() {
    if (!instTarget.value || !instForm.value.name.trim()) {
        message.warning("请填写员工姓名");
        return;
    }
    instSaving.value = true;
    try {
        await instantiateV2Post(instTarget.value.id, {
            name: instForm.value.name.trim(),
            position: instForm.value.position.trim(),
            bio: instForm.value.bio.trim(),
        });
        message.success("已从岗位创建 V2 员工");
        instOpen.value = false;
        activeTab.value = "members";
        await loadMembers();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "创建失败");
    } finally {
        instSaving.value = false;
    }
}

// ---- State: member edit drawer ----

const memberEditOpen = ref(false);
const memberEditing = ref<MemberEditForm | null>(null);
const memberSaving = ref(false);
const allSkills = ref<SkillOption[]>([]);
const memberPostSkills = ref<{ name: string; description: string }[]>([]);  // 员工所属岗位的推荐技能
const memberPlugins = ref<MemberPluginItem[]>([]);  // 按员工启用插件的绑定状态

async function loadMemberPlugins(memberId: string) {
    try {
        const res: any = await getMemberPluginBindings(memberId);
        memberPlugins.value = res?.plugins || [];
    } catch { memberPlugins.value = []; }
}

async function onPluginBindingChange(plugin: MemberPluginItem, mode: string) {
    if (!memberEditing.value?.id) return;
    const effective = (mode || null) as "allow" | "deny" | null;
    try {
        await setMemberPluginBinding(memberEditing.value.id, plugin.name, effective);
        plugin.binding_mode = effective;
        plugin.binding_source = effective ? "admin" : "";
        message.success(`「${plugin.display_name}」已${effective === null ? "恢复默认" : effective === "allow" ? "允许" : "关闭"}`);
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "保存插件设置失败");
    }
}

async function loadAllSkills() {
    try {
        const res: any = await getSkills();
        allSkills.value = (res?.skills || []).map((s: any) => ({
            id: s.id, name: s.name, description: s.description || "",
        }));
    } catch { /* ignore */ }
}

async function openEditMember(member: V2MemberItem) {
    try {
        const res: any = await getV2Member(member.id);
        memberEditing.value = {
            id: res.id, name: res.name, position: res.position || "", bio: res.bio || "",
            post_key: res.post || "",
            identity: res.identity, persona: res.persona, bible: res.bible,
            skill_ids: (res.skill_ids || []),
        };
        memberPostSkills.value = res.post_skills || [];
        memberEditOpen.value = true;
        if (res.id) await loadMemberPlugins(res.id);
    } catch { message.error("加载员工详情失败"); }
}

function openCreateMember() {
    memberEditing.value = {
        name: "", position: "", bio: "", post_key: "",
        identity: "", persona: "", bible: "", skill_ids: [],
    };
    memberPostSkills.value = [];
    memberPlugins.value = [];
    memberEditOpen.value = true;
}

function onMemberPostChange(postKey: string) {
    // 新建时选择岗位 → 预填三件套，仍可自由修改
    const form = memberEditing.value;
    if (!form || form.id || !postKey) return;
    const post = posts.value.find(p => p.key === postKey);
    if (!post) return;
    form.identity = post.identity;
    form.persona = post.persona;
    form.bible = post.bible;
    if (!form.position.trim()) form.position = post.name;
    if (!form.bio.trim()) form.bio = post.description;
}

async function handleSaveMember() {
    const form = memberEditing.value;
    if (!form?.name.trim()) {
        message.warning("请填写员工姓名");
        return;
    }
    if (!form.identity.trim() || !form.persona.trim() || !form.bible.trim()) {
        message.warning("IDENTITY / PERSONA / BIBLE 三份文件都不能为空");
        return;
    }
    memberSaving.value = true;
    try {
        if (form.id) {
            await updateV2Member(form.id, {
                name: form.name, position: form.position, bio: form.bio,
                identity: form.identity, persona: form.persona, bible: form.bible,
                skill_ids: form.skill_ids,
            });
            message.success("员工已更新");
        } else {
            await createV2Member({
                name: form.name.trim(), position: form.position.trim(), bio: form.bio.trim(),
                post_key: form.post_key || "",
                identity: form.identity, persona: form.persona, bible: form.bible,
                skill_ids: form.skill_ids,
            });
            message.success("员工已创建");
        }
        memberEditOpen.value = false;
        activeTab.value = "members";
        await loadMembers();
    } catch (e: any) {
        message.error(e?.response?.data?.detail || "保存失败");
    } finally {
        memberSaving.value = false;
    }
}

async function handleDeleteMember(member: V2MemberItem) {
    try {
        await deleteMember(member.id);
        message.success("已删除");
        await loadMembers();
    } catch { message.error("删除失败"); }
}

async function handleChat(member: V2MemberItem) {
    try {
        const res: any = await startMemberChat(member.id);
        if (res?.session_id) {
            router.push({ name: "chat", query: { session: res.session_id, contact: member.id, type: "member" } });
        }
    } catch { message.error("进入对话失败"); }
}

// ---- Init ----

onMounted(async () => {
    loading.value = true;
    await Promise.all([loadPosts(), loadMembers(), loadKbStats(), loadAllSkills()]);
    loading.value = false;
});
</script>

<template>
    <div class="space-y-5">
        <!-- Header -->
        <div class="flex items-center justify-between">
            <div>
                <h2 class="text-lg font-medium text-gray-800 flex items-center gap-2">
                    AI 员工 V2
                    <VortTag color="purple" :bordered="false" size="small">三件套</VortTag>
                </h2>
                <p class="text-sm text-gray-500 mt-1">
                    以 IDENTITY / PERSONA / BIBLE 三件套定义的数字员工，支持自然语言自助生成（技能库强化）
                </p>
            </div>
            <VortButton variant="primary" @click="openGenerate">
                <Sparkles :size="14" class="mr-1" /> 生成岗位 / 员工
            </VortButton>
        </div>

        <!-- Tabs -->
        <div class="flex items-center gap-3">
            <div class="flex gap-1 bg-gray-100 rounded-lg p-1">
                <button
                    class="px-4 py-1.5 text-sm rounded-md transition-colors"
                    :class="activeTab === 'posts' ? 'bg-white text-blue-600 font-medium shadow-sm' : 'text-gray-500 hover:text-gray-700'"
                    @click="activeTab = 'posts'"
                >
                    岗位库
                    <span class="ml-1 text-xs text-gray-400">{{ posts.length }}</span>
                </button>
                <button
                    class="px-4 py-1.5 text-sm rounded-md transition-colors"
                    :class="activeTab === 'members' ? 'bg-white text-blue-600 font-medium shadow-sm' : 'text-gray-500 hover:text-gray-700'"
                    @click="activeTab = 'members'"
                >
                    V2 员工
                    <span class="ml-1 text-xs text-gray-400">{{ members.length }}</span>
                </button>
            </div>
            <div class="text-xs text-gray-400 flex items-center gap-1">
                <BookOpen :size="13" />
                技能库强化：{{ kbStats.functions }} 职能 · {{ kbStats.skill_refs }} 技能素材 · {{ kbStats.mappings }} 映射
            </div>
            <div class="ml-auto flex items-center gap-2">
                <VortButton v-if="activeTab === 'posts'" size="small" @click="openCreatePost">
                    <Plus :size="13" class="mr-1" /> 手动新建岗位
                </VortButton>
                <VortButton v-else size="small" @click="openCreateMember">
                    <Plus :size="13" class="mr-1" /> 手动新建员工
                </VortButton>
            </div>
        </div>

        <VortSpin :spinning="loading">
            <!-- Posts tab -->
            <template v-if="activeTab === 'posts'">
                <div v-if="posts.length" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                    <div
                        v-for="post in posts" :key="post.id"
                        class="bg-white rounded-xl border border-gray-100 p-4 hover:shadow-md hover:border-gray-200 transition-all"
                    >
                        <div class="flex items-start gap-3">
                            <div class="w-10 h-10 rounded-lg bg-violet-50 flex items-center justify-center flex-shrink-0">
                                <Layers :size="20" class="text-violet-600" />
                            </div>
                            <div class="min-w-0 flex-1">
                                <div class="font-medium text-gray-800 truncate" :title="post.name">{{ post.name }}</div>
                                <code class="text-xs text-gray-400">{{ post.key }}</code>
                            </div>
                            <VortTag v-if="!post.enabled" size="small" color="default">已禁用</VortTag>
                        </div>
                        <div class="text-xs text-gray-500 mt-3 line-clamp-2 min-h-[32px]">
                            {{ post.description || '暂无描述' }}
                        </div>
                        <div v-if="post.skills?.length" class="flex flex-wrap gap-1 mt-2 min-h-[22px]">
                            <VortTag
                                v-for="s in post.skills.slice(0, 3)" :key="s.name"
                                size="small" color="cyan" :bordered="false" :title="s.description"
                            >{{ s.name }}</VortTag>
                            <VortTag v-if="post.skills.length > 3" size="small" color="cyan" :bordered="false">
                                +{{ post.skills.length - 3 }}
                            </VortTag>
                        </div>
                        <div class="flex items-center justify-end gap-1 mt-3 pt-3 border-t border-gray-50">
                            <VortButton size="small" @click="openEditPost(post)">
                                <Pencil :size="12" class="mr-1" /> 编辑
                            </VortButton>
                            <VortButton size="small" @click="openInstantiate(post)">
                                <UserPlus :size="12" class="mr-1" /> 实例化
                            </VortButton>
                            <VortPopconfirm title="确认禁用该岗位？" @confirm="handleDeletePost(post)">
                                <VortButton size="small" danger>
                                    <Trash2 :size="12" />
                                </VortButton>
                            </VortPopconfirm>
                        </div>
                    </div>
                </div>
                <div v-else-if="!loading" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
                    <Layers :size="48" class="mx-auto text-gray-300 mb-4" />
                    <div class="text-gray-500 mb-2">岗位库为空</div>
                    <div class="text-xs text-gray-400 mb-4">运行 scripts/migrate_skill_kb.py 导入 skill_kb 职位，或直接用自然语言生成</div>
                    <VortButton variant="primary" size="small" @click="openGenerate">
                        <Sparkles :size="14" class="mr-1" /> 生成第一个岗位
                    </VortButton>
                </div>
            </template>

            <!-- Members tab -->
            <template v-else>
                <div v-if="members.length" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                    <div
                        v-for="member in members" :key="member.id"
                        class="bg-white rounded-xl border border-gray-100 p-4 hover:shadow-md hover:border-gray-200 transition-all"
                    >
                        <div class="flex items-start gap-3">
                            <div class="w-10 h-10 rounded-lg bg-blue-50 flex items-center justify-center flex-shrink-0">
                                <Bot :size="20" class="text-blue-600" />
                            </div>
                            <div class="min-w-0 flex-1">
                                <div class="font-medium text-gray-800 truncate">{{ member.name }}</div>
                                <div class="text-xs text-gray-400 mt-0.5">{{ member.position || 'AI 员工' }}</div>
                            </div>
                        </div>
                        <div class="text-xs text-gray-500 mt-3 line-clamp-2 min-h-[32px]">
                            {{ member.bio || '暂无描述' }}
                        </div>
                        <div v-if="member.skill_ids.length" class="text-xs text-gray-400 mt-2 flex items-center gap-1">
                            <BadgeCheck :size="12" /> 已订阅 {{ member.skill_ids.length }} 个技能
                        </div>
                        <div class="flex items-center justify-end gap-1 mt-3 pt-3 border-t border-gray-50">
                            <VortButton size="small" @click="openEditMember(member)">
                                <Pencil :size="12" class="mr-1" /> 编辑
                            </VortButton>
                            <VortButton size="small" @click="handleChat(member)">
                                <MessageSquare :size="12" class="mr-1" /> 对话
                            </VortButton>
                            <VortPopconfirm title="确认删除该 V2 员工？删除后不可恢复。" @confirm="handleDeleteMember(member)">
                                <VortButton size="small" danger>
                                    <Trash2 :size="12" />
                                </VortButton>
                            </VortPopconfirm>
                        </div>
                    </div>
                </div>
                <div v-else-if="!loading" class="bg-white rounded-xl border border-gray-100 py-16 text-center">
                    <Bot :size="48" class="mx-auto text-gray-300 mb-4" />
                    <div class="text-gray-500 mb-2">暂无 V2 员工</div>
                    <div class="text-xs text-gray-400 mb-4">从岗位库实例化，或用自然语言直接生成</div>
                    <VortButton variant="primary" size="small" @click="openGenerate">
                        <Sparkles :size="14" class="mr-1" /> 生成第一个员工
                    </VortButton>
                </div>
            </template>
        </VortSpin>

        <!-- Generate drawer -->
        <VortDrawer v-model:open="genOpen" title="生成三件套（IDENTITY / PERSONA / BIBLE）" :width="760">
            <!-- Step 1: input -->
            <div v-if="!genResult" class="space-y-4">
                <VortForm label-width="80px">
                    <VortFormItem label="名称" required>
                        <VortInput v-model="genForm.name" placeholder="如：智能客服、财务会计、测试专家" />
                    </VortFormItem>
                    <VortFormItem label="职责描述">
                        <VortTextarea
                            v-model="genForm.description"
                            :rows="5"
                            placeholder="用一段自然语言描述这个岗位/员工要做什么、服务谁、边界在哪。描述越具体，生成质量越高。"
                        />
                    </VortFormItem>
                </VortForm>
                <div class="text-xs text-gray-400 flex items-center gap-1">
                    <BookOpen :size="13" />
                    生成时会从 {{ kbStats.functions }} 个职能中匹配素材，并推荐可订阅的平台技能
                </div>
                <div class="flex justify-end">
                    <VortButton variant="primary" :loading="genLoading" @click="handleGenerate">
                        <Sparkles :size="14" class="mr-1" /> 开始生成
                    </VortButton>
                </div>
            </div>

            <!-- Step 2: draft preview -->
            <div v-else class="space-y-4">
                <div class="flex items-center justify-between">
                    <div class="text-sm text-gray-500">
                        草稿未保存，可直接编辑；每份文件可单独重新生成
                    </div>
                    <div class="flex gap-2">
                        <VortButton size="small" @click="genResult = null">
                            <ArrowLeft :size="12" class="mr-1" /> 重填
                        </VortButton>
                        <VortButton size="small" :loading="genLoading" @click="handleGenerate">
                            <RefreshCw :size="12" class="mr-1" /> 全部重新生成
                        </VortButton>
                    </div>
                </div>

                <div v-if="genResult.matched_functions?.length" class="rounded-lg bg-violet-50/60 border border-violet-100 p-3">
                    <div class="text-xs font-medium text-violet-700 mb-2">匹配职能（生成素材来源）</div>
                    <div class="flex flex-wrap gap-1.5">
                        <VortTag
                            v-for="fn in genResult.matched_functions" :key="fn.function_id"
                            size="small" color="purple" :bordered="false"
                            :title="fn.positioning"
                        >{{ fn.name }}</VortTag>
                    </div>
                </div>

                <div v-for="meta in FIELD_META" :key="meta.key" class="rounded-lg border border-gray-100">
                    <div class="flex items-center justify-between px-3 py-2 border-b border-gray-100 bg-gray-50/60 rounded-t-lg">
                        <div>
                            <span class="text-sm font-semibold text-gray-700">{{ meta.title }}</span>
                            <span class="text-xs text-gray-400 ml-2">{{ meta.hint }}</span>
                        </div>
                        <VortButton
                            size="small"
                            :loading="regenField === meta.key"
                            :disabled="genLoading && regenField !== meta.key"
                            @click="handleRegenerate(meta.key)"
                        >
                            <RefreshCw :size="12" class="mr-1" /> 重新生成
                        </VortButton>
                    </div>
                    <VortTextarea
                        v-model="(genResult as any)[meta.key]"
                        :rows="9"
                        class="gen-textarea"
                    />
                </div>

                <div v-if="genResult.suggested_skills?.length" class="rounded-lg border border-gray-100 p-3">
                    <div class="text-sm font-semibold text-gray-700 mb-1">建议订阅的平台技能</div>
                    <div class="text-xs text-gray-400 mb-2">保存为员工时会自动订阅勾选的技能</div>
                    <VortSelect v-model="genSelectedSkills" mode="multiple" placeholder="选择要订阅的技能" style="width: 100%" :max-tag-count="6">
                        <VortSelectOption v-for="s in genResult.suggested_skills" :key="s.id" :value="s.id">
                            {{ s.name }}
                        </VortSelectOption>
                    </VortSelect>
                </div>

                <div class="flex justify-end gap-2 pt-2">
                    <VortButton @click="handleSaveAsPost">保存为岗位</VortButton>
                    <VortButton variant="primary" @click="handleSaveAsMember">保存为员工</VortButton>
                </div>
            </div>
        </VortDrawer>

        <!-- Post edit drawer -->
        <VortDrawer v-model:open="postEditOpen" :title="postEditing?.id ? '编辑 V2 岗位' : '手动新建 V2 岗位'" :width="720">
            <div v-if="postEditing" class="space-y-4">
                <VortForm label-width="80px">
                    <VortFormItem label="名称" required>
                        <VortInput v-model="postEditing.name" />
                    </VortFormItem>
                    <VortFormItem label="描述">
                        <VortInput v-model="postEditing.description" />
                    </VortFormItem>
                </VortForm>
                <div v-if="postEditing.skills?.length" class="rounded-lg bg-cyan-50/50 border border-cyan-100 p-3">
                    <div class="text-xs font-medium text-cyan-700 mb-2">岗位技能（来自 skill_kb 素材库，随迁移保留）</div>
                    <div class="flex flex-wrap gap-1.5">
                        <VortTag
                            v-for="s in postEditing.skills" :key="s.name"
                            size="small" color="cyan" :bordered="false" :title="s.description"
                        >{{ s.name }}</VortTag>
                    </div>
                </div>
                <div v-for="meta in FIELD_META" :key="meta.key" class="rounded-lg border border-gray-100">
                    <div class="px-3 py-2 border-b border-gray-100 bg-gray-50/60 rounded-t-lg">
                        <span class="text-sm font-semibold text-gray-700">{{ meta.title }}</span>
                    </div>
                    <VortTextarea v-model="(postEditing as any)[meta.key]" :rows="9" />
                </div>
                <div class="flex justify-end">
                    <VortButton variant="primary" :loading="postSaving" @click="handleSavePost">保存</VortButton>
                </div>
            </div>
        </VortDrawer>

        <!-- Instantiate drawer -->
        <VortDrawer v-model:open="instOpen" title="从岗位创建 V2 员工" :width="480">
            <div v-if="instTarget" class="space-y-4">
                <div class="text-sm text-gray-500">
                    将从岗位「{{ instTarget.name }}」创建一个 AI 员工，三件套人设原样继承，之后可单独调整。
                </div>
                <VortForm label-width="80px">
                    <VortFormItem label="员工姓名" required>
                        <VortInput v-model="instForm.name" placeholder="如：小客服、财务小助手" />
                    </VortFormItem>
                    <VortFormItem label="职位">
                        <VortInput v-model="instForm.position" placeholder="默认使用岗位名称" />
                    </VortFormItem>
                    <VortFormItem label="简介">
                        <VortTextarea v-model="instForm.bio" :rows="3" placeholder="默认使用岗位描述" />
                    </VortFormItem>
                </VortForm>
                <div class="flex justify-end">
                    <VortButton variant="primary" :loading="instSaving" @click="handleInstantiate">创建</VortButton>
                </div>
            </div>
        </VortDrawer>

        <!-- Member edit drawer -->
        <VortDrawer v-model:open="memberEditOpen" :title="memberEditing?.id ? '编辑 V2 员工' : '手动新建 V2 员工'" :width="720">
            <div v-if="memberEditing" class="space-y-4">
                <VortForm label-width="80px">
                    <VortFormItem label="姓名" required>
                        <VortInput v-model="memberEditing.name" />
                    </VortFormItem>
                    <VortFormItem label="职位">
                        <VortInput v-model="memberEditing.position" />
                    </VortFormItem>
                    <VortFormItem label="简介">
                        <VortTextarea v-model="memberEditing.bio" :rows="2" />
                    </VortFormItem>
                    <VortFormItem label="岗位">
                        <template v-if="!memberEditing.id">
                            <VortSelect
                                :model-value="memberEditing.post_key || ''"
                                placeholder="可选；选择后预填三件套，仍可自由修改"
                                style="width: 100%"
                                allow-clear
                                @update:model-value="(v: any) => { if (memberEditing) { memberEditing.post_key = v || ''; onMemberPostChange(v || ''); } }"
                            >
                                <VortSelectOption v-for="p in posts" :key="p.key" :value="p.key">
                                    {{ p.name }}
                                </VortSelectOption>
                            </VortSelect>
                        </template>
                        <template v-else>
                            <span class="text-sm text-gray-600">{{ posts.find(p => p.key === memberEditing!.post_key)?.name || memberEditing.post_key || '未绑定岗位' }}</span>
                        </template>
                    </VortFormItem>
                    <VortFormItem label="订阅技能">
                        <VortSelect
                            v-model="memberEditing.skill_ids"
                            mode="multiple"
                            placeholder="选择要订阅的平台技能"
                            style="width: 100%"
                            allow-clear
                            :max-tag-count="6"
                        >
                            <VortSelectOption v-for="s in allSkills" :key="s.id" :value="s.id">
                                {{ s.name }}
                            </VortSelectOption>
                        </VortSelect>
                    </VortFormItem>
                    <VortFormItem v-if="memberEditing.id" label="启用插件">
                        <div class="w-full space-y-1.5">
                            <div
                                v-for="p in memberPlugins" :key="p.name"
                                class="flex items-center gap-2"
                            >
                                <span class="text-sm text-gray-700 w-36 truncate" :title="p.description">{{ p.display_name }}</span>
                                <VortTag v-if="p.visibility === 'members'" size="small" color="purple" :bordered="false">仅绑定可见</VortTag>
                                <VortTag v-if="p.binding_source === 'auto'" size="small" color="green" :bordered="false">团队自动</VortTag>
                                <VortTag v-if="!p.enabled" size="small" color="default" :bordered="false">已全局禁用</VortTag>
                                <VortSelect
                                    :model-value="p.binding_mode || ''"
                                    style="flex: 1"
                                    @update:model-value="(v: any) => onPluginBindingChange(p, v || '')"
                                >
                                    <VortSelectOption value="">
                                        默认（{{ p.visibility === 'members' ? '不可见' : '可见' }}）
                                    </VortSelectOption>
                                    <VortSelectOption v-if="p.visibility === 'members'" value="allow">允许该员工使用</VortSelectOption>
                                    <VortSelectOption v-else value="deny">对该员工关闭</VortSelectOption>
                                </VortSelect>
                            </div>
                            <div v-if="!memberPlugins.length" class="text-xs text-gray-400">没有可按员工配置的插件（核心插件恒可见）</div>
                            <div v-else class="text-xs text-gray-400">改动即时生效；「团队自动」的绑定由团队负责人身份维护，手动改动后转为管理员配置</div>
                        </div>
                    </VortFormItem>
                </VortForm>
                <div v-if="!memberEditing.id && memberEditing.post_key" class="rounded-lg bg-blue-50/50 border border-blue-100 p-3 text-xs text-blue-600">
                    已从岗位「{{ posts.find(p => p.key === memberEditing!.post_key)?.name }}」预填三件套
                </div>
                <div v-if="memberEditing.id && memberPostSkills.length" class="rounded-lg bg-cyan-50/50 border border-cyan-100 p-3">
                    <div class="text-xs font-medium text-cyan-700 mb-2">岗位推荐技能（素材库，供参考）</div>
                    <div class="flex flex-wrap gap-1.5">
                        <VortTag
                            v-for="s in memberPostSkills" :key="s.name"
                            size="small" color="cyan" :bordered="false" :title="s.description"
                        >{{ s.name }}</VortTag>
                    </div>
                </div>
                <div v-for="meta in FIELD_META" :key="meta.key" class="rounded-lg border border-gray-100">
                    <div class="px-3 py-2 border-b border-gray-100 bg-gray-50/60 rounded-t-lg">
                        <span class="text-sm font-semibold text-gray-700">{{ meta.title }}</span>
                    </div>
                    <VortTextarea v-model="(memberEditing as any)[meta.key]" :rows="9" />
                </div>
                <div class="flex justify-end">
                    <VortButton variant="primary" :loading="memberSaving" @click="handleSaveMember">保存</VortButton>
                </div>
            </div>
        </VortDrawer>
    </div>
</template>
