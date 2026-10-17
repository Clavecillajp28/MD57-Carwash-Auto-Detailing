"""Optional Chromium regression for HTML entity decoding in dynamic actions."""
from pathlib import Path
import unittest


class BrowserActionsTests(unittest.TestCase):
    def test_peso_formatter_and_all_pricing_categories(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Install Playwright and Chromium for the browser regression.')
        source = Path(__file__).resolve().parents[1] / '_Frontend/static/js/common.js'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('<div id="pricingModalBody"></div>')
                page.add_script_tag(path=str(source))
                self.assertEqual(page.evaluate('[0,150,1000,null,undefined,""] .map(formatPeso)'),
                                 ['\u20b10', '\u20b1150', '\u20b11,000', 'N/A', 'N/A', 'N/A'])
                page.evaluate('''() => {
                    const services = [
                        {name:'Wash',prices:{Small:150,Large:1000},fixed_price:false},
                        {name:'Fixed',prices:{Small:500},fixed_price:true}
                    ];
                    _pricingModalCatalog = {
                        vehicle_types:[{type_name:'Small'},{type_name:'Large'}],
                        categories:Object.fromEntries(['general','detailing','motorcycle'].map(key=>[key,{services}]))
                    };
                }''')
                for category in ('general', 'detailing', 'motorcycle'):
                    page.evaluate('renderPricingModalCategory', category)
                    self.assertEqual(page.locator('tbody td:not(:first-child)').all_text_contents(),
                                     ['\u20b1150', '\u20b11,000'])
                    self.assertEqual(page.locator('.pricing-fixed-list strong').inner_text(), '\u20b1500')
            finally:
                browser.close()

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
