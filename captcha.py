import random
import string
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import base64

class CaptchaGenerator:
    def __init__(self, length=4):
         self.length = length

    def generate_captcha(self):
        """ Generating a random captch consisting of alpha character. """
        return ''.join(random.choices(string.ascii_uppercase, k=self.length))

    def generate_captcha_image(self, captcha_text):
        width, height = 200, 60
        image = Image.new('RGB', (width, height), color='white')
        draw = ImageDraw.Draw(image)

        font = ImageFont.truetype("DejaVuSans-ExtraLight.ttf", size=36)

        #text_width, text_height = draw.textsize(captcha_text, font=font)
        bbox = draw.textbbox((0,0), captcha_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (width - text_width) // 2
        y = (height - text_height) // 2
        draw.text((x, y), captcha_text, font=font, fill='green')

        for _ in range(5):
            x1, y1 = random.randint(0, width), random.randint(0, height)
            x2, y2 = random.randint(0, width), random.randint(0, height)
            draw.line(((x1, y1), (x2, y2)), fill='gray', width=1)

        for _ in range(30):
            x, y = random.randint(0, width), random.randint(0, height)
            draw.ellipse((x, y, x+2, y+2), fill='gray')


            buffer = BytesIO()
            image.save(buffer, format='PNG')
            buffer.seek(0)
       
        return base64.b64encode(buffer.getvalue()).decode('utf-8')
