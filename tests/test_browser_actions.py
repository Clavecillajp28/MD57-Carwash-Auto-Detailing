"""Optional Chromium regression for HTML entity decoding in dynamic actions."""
from pathlib import Path
import unittest


class BrowserActionsTests(unittest.TestCase):
    def test_entity_quote_remains_data_and_keyboard_actions_work(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Install Playwright and Chromium for the browser regression.')
        source=Path(__file__).resolve().parents[1]/'_Frontend/static/js/common.js'
        payload="&#39;);window.auditMarker=1;//"
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                page.set_content('<div id="target"></div>')
                page.add_script_tag(path=str(source))
                page.evaluate('''value => {
                    window.auditMarker=0;window.received=[];
                    registerActions({capture:(data)=>window.received.push(data)});
                    document.getElementById('target').innerHTML=`<button ${actionAttributes('capture',[value])}>Click</button><div role="button" tabindex="0" ${actionAttributes('capture',[value])}>Keyboard</div>`;
                }''',payload)
                page.get_by_role('button',name='Click',exact=True).click()
                page.get_by_role('button',name='Keyboard',exact=True).focus()
                page.keyboard.press('Enter')
                self.assertEqual(page.evaluate('window.auditMarker'),0)
                self.assertEqual(page.evaluate('window.received'),[payload,payload])
            finally:
                browser.close()
