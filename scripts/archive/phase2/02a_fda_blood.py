# Define target mappings based on the provided HTML value attributes
target_establishment_types = {
    "4": "Product_Testing_Laboratory",
    "3": "Plasmapheresis_Center",
    "6": "Component_Preparation_Facility"
}

# Base landing URL for the blood registration directory
blood_db_url = "https://www.accessdata.fda.gov/scripts/cber/CFAppsPub/Index.cfm"

print("Initializing automated web extraction loop...")

# Run through each type one by one
for type_value, type_name in target_establishment_types.items():
    print("\n" + "="*60)
    print(f"STARTING HARVEST FOR: {type_name.replace('_', ' ')}")
    print("="*60)
    
    driver = webdriver.Chrome()
    all_data = []
    page_num = 1
    
    try:
        # 1. Access the main search portal
        driver.get(blood_db_url)
        time.sleep(3)
        
        # 2. Select the specific Establishment Type dropdown option
        print(f"Setting Establishment Type value to '{type_value}'...")
        est_dropdown = Select(driver.find_element(By.NAME, "EstablishmentType"))
        est_dropdown.select_by_value(type_value)
        time.sleep(1)
        
        # 3. Handle Country criteria: Clear options and explicitly lock down 'UNITED STATES'
        print("Configuring location parameters to 'UNITED STATES'...")
        country_dropdown = Select(driver.find_element(By.NAME, "Country"))
        country_dropdown.deselect_all()
        country_dropdown.select_by_value("US")
        time.sleep(1)
        
        # 4. Maximize efficiency by requesting 100 records per page layout
        try:
            print("Requesting 100 records per page payload view...")
            records_dropdown = Select(driver.find_element(By.NAME, "nrecords"))
            records_dropdown.select_by_value("100")
        except Exception as e:
            print("Could not alter layout setting. Defaulting to standard page limits.")
            
        # 5. Dispatch form submission query
        search_button = driver.find_element(By.XPATH, "//input[@type='submit']")
        search_button.click()
        print("Query submitted! Awaiting landing page response data...")
        time.sleep(5)
        
        # 6. Pagination Extraction Loop
        while True:
            html_source = driver.page_source
            parsed_tables = pd.read_html(io.StringIO(html_source))
            
            target_grid = None
            
            # Identify the target grid containing actual database content
            for table in parsed_tables:
                table.columns = [str(col).strip().replace('\n', ' ') for col in table.columns]
                if 'FEI' in table.columns or 'Establishment Status' in table.columns:
                    target_grid = table
                    break
            
            if target_grid is not None:
                all_data.append(target_grid)
                print(f"   Processed Page {page_num}: Captured {len(target_grid)} data rows.")
            else:
                print("   Data structure missing. Stopped tracking at this boundary context.")
                break
                
            # Locate and click the 'Display next' action button
            try:
                next_page_btn = driver.find_element(By.ID, "Display next")
                next_page_btn.click()
                page_num += 1
                time.sleep(5)
            except:
                print("   🏁 Reached final page boundary. No additional navigation paths detected.")
                break
                
        # 7. Compile and dump the array contents to storage
        if all_data:
            master_df = pd.concat(all_data, ignore_index=True)
            master_df = master_df.drop_duplicates()
            
            output_filename = f"data/phase2_pharma/raw/fda_blood_{type_name.lower()}_us.csv"
            master_df.to_csv(output_filename, index=False)
            print(f"\n EXPORT SUCCESS: Saved {len(master_df)} clean rows to '{output_filename}'")
        else:
            print(f"\n Result array empty. No entries located matching specific criteria parameters.")
            
    except Exception as error_msg:
        print(f"\n Thread Execution Failure for segment target '{type_name}': {error_msg}")
        
    finally:
        driver.quit()

print("\n" + "="*60)
print(" COMPILATION CYCLE FINISHED: All commercial target datasets mapped!")
print("="*60)