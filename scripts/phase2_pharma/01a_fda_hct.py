from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
import pandas as pd
import time
import io

print("Opening Chrome browser...")
# Initialize the browser
driver = webdriver.Chrome()

try:
    # 1. Go to the front door
    driver.get("https://www.accessdata.fda.gov/scripts/cber/CFAppsPub/tiss/Index.cfm")
    time.sleep(3) # Let the page load

    # 2. Handle the Establishment Function menu
    dropdown = driver.find_element(By.ID, "Establishment Function")
    process_option = dropdown.find_element(By.XPATH, "//option[@value='e']")
    driver.execute_script("arguments[0].scrollIntoView();", process_option)
    time.sleep(1)
    process_option.click()
    
    if process_option.is_selected():
        print("SUCCESS: 'Process' is visually highlighted and selected!")
    else:
        print("WARNING: Browser rejected the click. Trying JavaScript force...")
        driver.execute_script("arguments[0].selected = true;", process_option)

    # 3. Let's make our lives easier by setting it to 100 records per page
    try:
        print("Setting view to 100 records per page...")
        records_dropdown = Select(driver.find_element(By.NAME, "nrecords"))
        records_dropdown.select_by_value("100")
        print("Success: Set to 100 records per page!")
    except Exception as e:
        print("Could not click 100 records. Defaulting to 10.")

    # 4. Click the Search button (Using a broad locator for the submit button)
    search_button = driver.find_element(By.XPATH, "//input[@type='submit']")
    search_button.click()
    print("Clicked Search! Waiting for the server...")
    time.sleep(5) 

    all_data = []
    page_num = 1

    # 5. Loop through the pages
    while True:
        # FIX: Wrap HTML in io.StringIO to remove the Pandas warning
        html = driver.page_source
        tables = pd.read_html(io.StringIO(html))
        
        target_table = None
        
        # Verify we are grabbing the REAL data table, not the website layout
        for t in tables:
            t.columns = [str(c).strip().replace('\n', ' ') for c in t.columns]
            if 'FEI' in t.columns or 'Establishment Status' in t.columns:
                target_table = t
                break
                
        if target_table is not None:
            all_data.append(target_table)
            print(f"Scraped Page {page_num}... got {len(target_table)} rows.")
        else:
            print("Could not find the FDA data table. We might be stuck on the search page.")
            break
        
        # 6. Find and click the "Next" button
        try:
            next_button = driver.find_element(By.ID, "Display next")
            print(f"Found button: '{next_button.get_attribute('value')}'. Clicking to next page...")
            next_button.click()
            page_num += 1
            time.sleep(5)
        except:
            print("No more 'Next' buttons found! Scraping complete.")
            break

    # 7. Combine and save the data
    if all_data:
        final_df = pd.concat(all_data, ignore_index=True)
        final_df.to_csv("data/phase2_pharma/raw/fda_biologics_hctp_processed.csv", index=False)
        print(f"\nSUCCESS! Saved {len(final_df)} commercial biologics manufacturers to CSV!")

except Exception as e:
    print(f"An error occurred: {e}")

finally:
    driver.quit()