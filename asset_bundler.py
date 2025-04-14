"""
Asset bundling and minification for the WorkVista application.
"""
import os
import re
import logging
from pathlib import Path

logger = logging.getLogger('asset_bundler')

def minify_css(css_content):
    """
    Minify CSS content by removing comments, whitespace, and unnecessary characters.
    
    Args:
        css_content (str): The CSS content to minify
        
    Returns:
        str: Minified CSS content
    """
    # Remove comments
    css_content = re.sub(r'/\*[\s\S]*?\*/', '', css_content)
    
    # Remove whitespace around selectors and declarations
    css_content = re.sub(r'\s*{\s*', '{', css_content)
    css_content = re.sub(r'\s*}\s*', '}', css_content)
    css_content = re.sub(r'\s*;\s*', ';', css_content)
    css_content = re.sub(r'\s*:\s*', ':', css_content)
    css_content = re.sub(r'\s*,\s*', ',', css_content)
    
    # Remove last semicolon in each declaration block
    css_content = re.sub(r';}', '}', css_content)
    
    # Remove extra whitespace and line breaks
    css_content = re.sub(r'\s+', ' ', css_content)
    css_content = css_content.strip()
    
    return css_content

def process_css_files(app):
    """
    Process CSS files and create minified versions.
    
    Args:
        app: The Flask application instance
    """
    static_folder = Path(app.static_folder)
    css_folder = static_folder / 'css'
    minified_folder = css_folder / 'min'
    
    # Create minified directory if it doesn't exist
    os.makedirs(minified_folder, exist_ok=True)
    
    # Process each CSS file
    for css_file in css_folder.glob('*.css'):
        if css_file.name.endswith('.min.css'):
            continue
            
        output_file = minified_folder / f"{css_file.stem}.min.css"
        
        try:
            with open(css_file, 'r', encoding='utf-8') as f:
                css_content = f.read()
                
            minified_content = minify_css(css_content)
            
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(minified_content)
                
            # Calculate reduction percentage
            original_size = len(css_content)
            minified_size = len(minified_content)
            reduction = (original_size - minified_size) / original_size * 100
            
            logger.info(f"Minified {css_file.name}: {original_size:,} bytes → {minified_size:,} bytes ({reduction:.1f}% reduction)")
            
        except Exception as e:
            logger.error(f"Error processing {css_file}: {str(e)}")
    
    logger.info(f"CSS minification complete. Files saved to {minified_folder}")
    
def init_app(app):
    """
    Initialize asset bundling with the Flask app.
    
    Args:
        app: The Flask application instance
    """
    # Process CSS files when the app starts
    with app.app_context():
        process_css_files(app)
        
    # Add context processor to make asset_url available in templates
    @app.context_processor
    def inject_asset_url():
        def asset_url(filename):
            """
            Generate URL for static assets with versioning for cache busting.
            Use minified version in production.
            
            Args:
                filename (str): Asset filename (e.g., 'css/style.css')
                
            Returns:
                str: URL to the asset with version parameter
            """
            if app.debug:
                # In debug mode, use original files
                url = filename
            else:
                # In production, use minified files if available
                if filename.startswith('css/') and not filename.endswith('.min.css'):
                    path_parts = filename.split('/')
                    if len(path_parts) > 1:
                        file_part = path_parts[-1]
                        path_parts[-1] = f"min/{file_part.rsplit('.', 1)[0]}.min.css"
                        url = '/'.join(path_parts)
                    else:
                        url = filename
                else:
                    url = filename
            
            # Add timestamp for cache busting
            static_folder = Path(app.static_folder)
            full_path = static_folder / url.lstrip('/')
            
            if full_path.exists():
                mtime = int(os.path.getmtime(full_path))
                return f"/static/{url}?v={mtime}"
            
            # Fallback to original URL if file doesn't exist
            return f"/static/{filename}"
        
        return dict(asset_url=asset_url)