from flask import Flask, request, render_template, current_app, url_for, Blueprint, flash, redirect, session
import logging
from flask import current_app


bp = Blueprint('info', __name__)


@bp.route('/optical_lens_comparison')
def lens_comparison():
    return render_template('optical_lens_comparison.html')
