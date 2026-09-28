import type { HarnessSpec } from '../../../../schema/schemas/primitives/harness';
import { StrandsRenderer } from '../../../templates/StrandsRenderer';
import type { AgentRenderConfig } from '../../../templates/types';
import { mapHarnessToExportConfig } from '../harness-mapper';
import type { ResolvedHarnessContext } from '../types';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

function exportContext(): ResolvedHarnessContext {
  const spec = {
    name: 'TestHarness',
    model: { provider: 'bedrock', modelId: 'global.anthropic.claude-sonnet-4-6' },
    tools: [],
    skills: [],
    allowedTools: ['shell', 'file_operations', 'web_fetch', 'todos', 'context_offloader', 'subagent'],
  } as HarnessSpec;

  return {
    harnessName: spec.name,
    targetAgentName: 'TestAgent',
    spec,
    systemPrompt: 'Use the exported Harness behavior.',
    projectSpec: { name: 'project', runtimes: [], memories: [], credentials: [], harnesses: [] } as any,
    deployedResources: null,
    configBaseDir: '/project/agentcore',
    projectRoot: '/project',
    exportNotes: [],
    region: 'us-east-1',
    localEnvVars: {},
    generatedPolicyFiles: {},
    additionalPolicies: [],
  };
}

function ordinaryAgentConfig(): AgentRenderConfig {
  return {
    name: 'OrdinaryAgent',
    sdkFramework: 'Strands',
    targetLanguage: 'Python',
    modelProvider: 'Bedrock',
    hasMemory: false,
    hasIdentity: false,
    hasGateway: false,
    hasPayment: false,
    isVpc: false,
    buildType: 'CodeZip',
    memoryProviders: [],
    identityProviders: [],
    gatewayProviders: [],
    gatewayAuthTypes: [],
    protocol: 'HTTP',
    maxIterations: 2,
  };
}

describe('Harness export rendering', () => {
  const outputDirs: string[] = [];

  afterEach(() => {
    for (const dir of outputDirs.splice(0)) {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  async function render(config: AgentRenderConfig): Promise<{
    agentDir: string;
    main: string;
    model: string;
    pyproject: string;
    harnessRuntime?: string;
    mcpClient?: string;
    executionLimits?: string;
  }> {
    const outputDir = mkdtempSync(join(tmpdir(), 'harness-render-'));
    outputDirs.push(outputDir);
    await new StrandsRenderer(config).render({ outputDir });
    const agentDir = join(outputDir, 'app', config.name);
    return {
      agentDir,
      main: readFileSync(join(agentDir, 'main.py'), 'utf8'),
      model: readFileSync(join(agentDir, 'model', 'load.py'), 'utf8'),
      pyproject: readFileSync(join(agentDir, 'pyproject.toml'), 'utf8'),
      ...(existsSync(join(agentDir, 'harness_runtime', 'managed.py'))
        ? { harnessRuntime: readFileSync(join(agentDir, 'harness_runtime', 'managed.py'), 'utf8') }
        : {}),
      ...(existsSync(join(agentDir, 'mcp_client', 'client.py'))
        ? { mcpClient: readFileSync(join(agentDir, 'mcp_client', 'client.py'), 'utf8') }
        : {}),
      ...(existsSync(join(agentDir, 'hooks', 'execution_limits.py'))
        ? { executionLimits: readFileSync(join(agentDir, 'hooks', 'execution_limits.py'), 'utf8') }
        : {}),
    };
  }

  it('renders explicit Strands Harness capabilities without exposing an internal behavior profile', async () => {
    const { renderConfig } = mapHarnessToExportConfig(exportContext(), 'CodeZip');
    const { agentDir, main, model, pyproject, harnessRuntime } = await render(renderConfig);

    expect(main).toContain('from strands_harness import create_harness');
    expect(main).toContain('from harness_runtime import (');
    expect(main).toContain('agent = create_harness(');
    expect(main).toContain('build_managed_builtin_tools(["shell","read","write","edit","web_fetch"])');
    expect(main).toContain('enable_subagent=True');
    expect(main).toContain('builtin_tools=[]');
    expect(main).toContain('builtin_plugins=["todos"]');
    expect(main).toContain('background_tasks=False');
    expect(main).toContain('caching=False');
    expect(main).toContain('context_manager=False');
    expect(main).toContain('session=False');
    expect(main).toContain('skills=False');
    expect(main).toContain('memory=False');
    expect(main).toContain('ManagedToolProvider(');
    expect(harnessRuntime).toContain('make_web_fetch(mode="markdown")');
    expect(harnessRuntime).toContain('class AliasedAgentTool(AgentTool)');
    expect(harnessRuntime).toContain('name.replace("-", "_")');
    expect(harnessRuntime).toContain('make_subagent(');
    expect(harnessRuntime).toContain('_INHERITABLE_TOOL_TYPES = {"builtin", "remote_mcp", "agentcore_gateway"}');
    expect(harnessRuntime).not.toContain('consumer_plugins_by_tool');
    expect(harnessRuntime).toContain('_OFFLOAD_ROOT = "/tmp/loopy/offloaded"');
    expect(harnessRuntime).toContain('_MAX_OFFLOAD_BYTES = 8 * 1024 * 1024 * 1024');
    expect(harnessRuntime).toContain('max_result_tokens=1500');
    expect(harnessRuntime).toContain('preview_tokens=750');
    expect(harnessRuntime).toContain('evict_after_cycles=None');
    expect(main).not.toContain('def file_operations(');
    expect(main).not.toMatch(/profile/i);
    expect(model).toContain('cache_config=CacheConfig(');
    expect(model).toContain('system_prompt_ttl=True');
    expect(model).toContain('tools_ttl=True');
    expect(pyproject).toContain('"strands-agents[web-fetch] >= 1.56.0, < 2.0.0"');
    expect(pyproject).toContain('"strands-harness == 0.1.2"');
    expect(() => execFileSync('python3', ['-m', 'py_compile', join(agentDir, 'main.py')])).not.toThrow();
    expect(() =>
      execFileSync('python3', ['-m', 'py_compile', join(agentDir, 'harness_runtime', 'managed.py')])
    ).not.toThrow();
  });

  it('leaves ordinary Strands generation on Agent without the Harness dependency', async () => {
    const { main, model, pyproject, harnessRuntime, executionLimits } = await render(ordinaryAgentConfig());

    expect(main).toContain('from strands import Agent, tool');
    expect(main).toContain('return Agent(**kwargs)');
    expect(main).not.toContain('strands_harness');
    expect(main).not.toContain('return create_harness(');
    expect(model).not.toContain('CacheConfig');
    expect(pyproject).toContain('"strands-agents >= 1.15.0"');
    expect(pyproject).not.toContain('strands-harness');
    expect(harnessRuntime).toBeUndefined();
    expect(executionLimits).not.toContain('InvocationBudget');
    expect(executionLimits).not.toContain('AfterModelCallEvent');
    expect(executionLimits).toContain('event.agent.event_loop_metrics.accumulated_usage');
  });

  it('renders collision-aware MCP, skills, and shared execution-limit wiring as valid Python', async () => {
    const context = exportContext();
    context.spec = {
      ...context.spec,
      maxIterations: 4,
      maxTokens: 1000,
      skills: [{ path: '/opt/skills' }],
      tools: [
        {
          type: 'inline_function',
          name: 'shell',
          config: {
            inlineFunction: {
              description: 'Customer tool that collides with the managed shell.',
              inputSchema: { type: 'object', properties: {} },
            },
          },
        },
        {
          type: 'remote_mcp',
          name: 'docs',
          config: { remoteMcp: { url: 'https://example.com/mcp' } },
        },
      ],
      allowedTools: ['*'],
    } as HarnessSpec;

    const { renderConfig } = mapHarnessToExportConfig(context, 'CodeZip');
    const { agentDir, main, mcpClient, executionLimits } = await render(renderConfig);

    expect(main).toContain('ManagedToolEntry(namespace, tool_type, tool)');
    expect(main).toContain('"shell", "inline_function"');
    expect(main).toContain('skill_paths=skill_plugins or []');
    expect(mcpClient).toContain('application_name="docs"');
    expect(executionLimits).toContain('class InvocationBudget:');
    expect(executionLimits).toContain('def fork(self) -> "ExecutionLimitsHook":');
    expect(executionLimits).toContain('AfterModelCallEvent');

    for (const relativePath of [
      'main.py',
      join('mcp_client', 'client.py'),
      join('hooks', 'execution_limits.py'),
      join('harness_runtime', 'managed.py'),
    ]) {
      expect(() => execFileSync('python3', ['-m', 'py_compile', join(agentDir, relativePath)])).not.toThrow();
    }
  });
});
