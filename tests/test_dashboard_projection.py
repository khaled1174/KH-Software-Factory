"""Read-only dashboard projections preserve unknowns and accessible light-theme ink."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / '.claude/skills/kh/apps/visualizer'
BUN = os.environ.get('KH_BUN') or shutil.which('bun')


def contrast(foreground, background):
    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))
    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + .05) / (darker + .05)


class DashboardLightThemeTests(unittest.TestCase):
    def test_text_and_status_ink_contrast_on_light_surfaces(self):
        tokens = dict(re.findall(r'--([a-z0-9-]+):\s*(#[0-9a-f]{6})', (APP / 'src/style.css').read_text()))
        for ink in ('text', 'dim', 'faint', 'green', 'red', 'blue', 'amber', 'purple', 'cyan', 'violet'):
            for surface in ('panel', 'panel-2', 'panel-3', 'accent-bg', 'success-bg', 'danger-bg', 'warning-bg'):
                with self.subTest(ink=ink, surface=surface):
                    self.assertGreaterEqual(contrast(tokens[ink], tokens[surface]), 4.5)


@unittest.skipUnless(BUN, 'Bun is unavailable; set KH_BUN to test dashboard projections')
class DashboardProjectionTests(unittest.TestCase):
    def evaluate(self, body):
        imports = '\n'.join(f'import * as {name} from {json.dumps(str(APP / path))};' for name, path in (
            ('project', 'src/lib/project.ts'), ('events', 'src/lib/events.ts'), ('format', 'src/lib/format.ts')))
        checked = subprocess.run([BUN, '-e', imports + '\n' + body], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, checked.returncode, checked.stderr)
        return json.loads(checked.stdout)

    def test_unknown_measurements_remain_distinct_from_measured_zero(self):
        values = self.evaluate('console.log(JSON.stringify([project.observedCount(null), project.observedCount(undefined), project.observedCount(0), project.observedPercent(null), project.observedPercent(0), project.observedPercent(125), project.observedPercent(NaN)]));')
        self.assertEqual(['Not recorded', 'Not recorded', '0', 'Not recorded', '0%', 'Not recorded', 'Not recorded'], values)

    def test_cost_is_unknown_or_partial_until_receipt_is_complete(self):
        values = self.evaluate('console.log(JSON.stringify([format.fmtKnownCost(undefined, false), format.fmtKnownCost(0, false), format.fmtKnownCost(1.25, false), format.fmtKnownCost(0, true)]));')
        self.assertEqual(['Unknown', 'Unknown', 'Partial $1.25', '$0.0000'], values)

    def test_search_and_status_filters_use_recorded_task_state(self):
        values = self.evaluate('''
const tasks = [
  {id:'T1',title:'Pipe cleanup',status:'completed',summary:'Reader closed'},
  {id:'T2',title:'Test evidence',status:'needs_repair',blocker:'Missing command receipt'},
  {id:'T3',title:'Follow up',status:'blocked',blocker:'Depends on T2'},
  {id:'T4',title:'Review cleanup',status:'reviewing'},
  {id:'T5',title:'Future task',status:'pending'}
];
const ids = (query, status) => project.filterTasks(tasks, query, status).map(task => task.id);
console.log(JSON.stringify([ids('', 'attention'), ids('  RECEIPT  ', 'active'), ids('cleanup', 'completed'), ids('T3', 'all'), ids('', 'pending'), ids('missing', 'completed')]));
''')
        self.assertEqual([['T2', 'T3'], ['T2'], ['T1'], ['T3'], ['T5'], []], values)

    def test_pastel_role_colors_are_readable_without_changing_stored_colors(self):
        values = self.evaluate('''
const colors = ['#ffffff', '#c89bff', '#5ad2dd', '#e8b64a', '#000000'];
console.log(JSON.stringify({colors, displayed: colors.map(color => events.agentColor(color, null, 0))}));
''')
        self.assertEqual(['#ffffff', '#c89bff', '#5ad2dd', '#e8b64a', '#000000'], values['colors'])
        for color in values['displayed']:
            self.assertGreaterEqual(contrast(color, '#f1f4f8'), 4.5)
        self.assertEqual('#000000', values['displayed'][-1])


if __name__ == '__main__':
    unittest.main()
