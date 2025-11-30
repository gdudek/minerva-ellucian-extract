# minerva-ellucian-extract
Extract and save data from the Minerva (Ellucian 1.10) web interface and save it locally as PDF, text and sql.

This program scrapes all the data from the Minerva (Ellucian) “View all requests page”, opens each request in turn, and saves the data.  It is attached here.  

# Usage
Run Chome with debugging port on (see below)
Install packages from requirements.txt
python3  allViewButtons-basic.py 

# Run Chome with debugging port
To automate Chrome, you first need to launch it to enable remote control. On MacOS it’s as follows, and simular on other platforms.
 
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \ 
      --remote-debugging-port=9222 \ 
      --user-data-dir=/tmp/chrome-minerva-profile 

# Database browsing
  How to use the SQL database:
      - Each processed report has a requests.id. Find it (e.g., SELECT id, reference_num, start_date FROM requests;).
      - Get all items for that report:
        SELECT * FROM summary_items WHERE request_id = ? ORDER BY row_order;
      - Totals/Grand Total/Due to Claimant are marked with row_type='total'; line items have row_type='item'.

Tested only on Minerva with Ellucian 1.10 (Nov 2025)

Git repo:  https://github.com/gdudek/minerva-ellucian-extract

