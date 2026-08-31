"""Structured data on one host must not name the other host.

Google reads JSON-LD as the page's own claim about itself. A `.in` page whose
CollectionPage url, breadcrumb items or publisher logo point at optiwar.com
tells Google the canonical thing lives on the other domain — which is how a
regional store donates its own rankings. The templates express the host with
`site_url`, so a literal domain in a ld+json block is the defect, and this
test finds it in every template rather than in the two we happened to notice.
"""

import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LD_JSON = re.compile(
    r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
HOST = re.compile(r'https?://(?:www\.|in\.)?optiwar\.(?:com|in)\S*')


class JsonLdSiteIsolation(unittest.TestCase):
    def test_no_template_hardcodes_a_host_in_structured_data(self):
        offenders = []
        for path in sorted(glob.glob(
                os.path.join(REPO, 'templates', '**', '*.html'),
                recursive=True)):
            with open(path, encoding='utf-8') as fh:
                src = fh.read()
            for block in LD_JSON.findall(src):
                for found in HOST.findall(block):
                    offenders.append(
                        '%s: %s' % (os.path.relpath(path, REPO), found))
        self.assertEqual(offenders, [], (
            'JSON-LD must derive its host from site_url, so the same template '
            'describes optiwar.com on .com and optiwar.in on .in:\n  %s'
            % '\n  '.join(offenders)))

    def test_the_two_reported_pages_use_site_url(self):
        # The daily report named these; keep them named so a revert is loud.
        for name in ('all_frames.html', 'guide_frame_shapes.html'):
            with open(os.path.join(REPO, 'templates', name),
                      encoding='utf-8') as fh:
                src = fh.read()
            blocks = ''.join(LD_JSON.findall(src))
            self.assertIn('{{ site_url }}', blocks, name)


if __name__ == '__main__':
    unittest.main()
