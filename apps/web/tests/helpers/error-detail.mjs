// errorDetail lives in TaskPage.tsx (a React module); transpile just that export for a pure test.
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import * as config from '../../src/features/autonomous/config.ts';
import { ApiError } from '../../src/api-client/client.ts';

const source = await readFile(new URL('../../src/features/autonomous/TaskPage.tsx', import.meta.url), 'utf8');
const start = source.indexOf('export const errorDetail');
const end = source.indexOf(';\n', start) + 1;
const snippet = source.slice(start, end).replace('export const', 'const');
const code = ts.transpileModule(snippet, { compilerOptions: { module: ts.ModuleKind.None, isolatedModules: false } }).outputText;
export const errorDetail = new Function('ApiError', 'errorExplanation', `${code}; return errorDetail;`)(ApiError, config.errorExplanation);
